"""Manifest-backed repository reads and the single WAL publication protocol."""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from errors import ForgeError
from layout import repository_config
from object_store import (
    AmbiguousWrite,
    Create,
    GetResult,
    ObjectMeta,
    ObjectNotFound,
    ObjectStore,
    PreconditionFailed,
    Update,
    Version,
)
from object_store_fs import FilesystemObjectStore
from wal import (
    Manifest,
    ReplayState,
    WalHead,
    apply_record,
    canonical_json,
    event_id,
    sha256,
    validate_descriptor,
    validate_record,
)


@dataclass(frozen=True)
class AlreadyCommitted:
    seq: int


@dataclass
class Candidate:
    """An event prepared against one exact manifest generation."""

    event_fingerprint: dict
    payload: dict
    prerequisites: tuple[Callable[["Repository"], None], ...] = ()
    cleanup: Callable[[], None] | None = None

    @property
    def event_id(self) -> str:
        return event_id(self.event_fingerprint)


def _config(root: Path) -> dict:
    try:
        raw = repository_config(root).read_bytes()
        value = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ForgeError(f"cannot read repository configuration: {error}") from error
    expected = {"repository_id", "canonical_branch", "object_store"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ForgeError("repository configuration has an invalid shape", "integrity")
    if (
        not isinstance(value["repository_id"], str)
        or len(value["repository_id"]) != 32
        or any(char not in "0123456789abcdef" for char in value["repository_id"])
    ):
        raise ForgeError("repository configuration has an invalid repository ID", "integrity")
    if not isinstance(value["canonical_branch"], str) or not value["canonical_branch"]:
        raise ForgeError("repository configuration has an invalid branch", "integrity")
    store = value["object_store"]
    if not isinstance(store, dict) or set(store) != {"kind", "path"}:
        raise ForgeError("repository configuration has an invalid object store", "integrity")
    if store["kind"] != "filesystem" or not isinstance(store["path"], str) or not store["path"]:
        raise ForgeError("repository configuration has an unsupported object store", "integrity")
    if canonical_json(value) != raw:
        raise ForgeError("repository configuration is not canonical JSON", "integrity")
    return value


def write_repository_config(
    root: Path, repository_id: str, canonical_branch: str, object_store_path: Path
) -> dict:
    try:
        rendered = object_store_path.relative_to(root).as_posix()
    except ValueError:
        rendered = str(object_store_path)
    value = {
        "repository_id": repository_id,
        "canonical_branch": canonical_branch,
        "object_store": {"kind": "filesystem", "path": rendered},
    }
    target = repository_config(root)
    encoded = canonical_json(value)
    if target.exists():
        if target.read_bytes() != encoded:
            raise ForgeError("repository configuration conflicts with existing root")
        return value
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(encoded)
    with temporary.open("rb") as output:
        os.fsync(output.fileno())
    os.replace(temporary, target)
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return value


class Repository:
    """One object-store namespace and its manifest-CAS publication loop."""

    def __init__(self, root: Path, store: ObjectStore, repository_id: str):
        self.root = root
        self.store = store
        self.repository_id = repository_id
        self.writer = uuid.uuid4().hex
        self.prefix = f"repos/{repository_id}"

    @classmethod
    def open(cls, root: Path, store: ObjectStore | None = None) -> "Repository":
        config = _config(root)
        if store is None:
            configured = Path(config["object_store"]["path"])
            if not configured.is_absolute():
                configured = root / configured
            store = FilesystemObjectStore(configured)
        return cls(root, store, config["repository_id"])

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}"

    def get(self, key: str, **kwargs) -> GetResult:
        result = self.store.get(self._key(key), **kwargs)
        return GetResult(result.body, ObjectMeta(key, result.meta.size, result.meta.version))

    def head(self, key: str) -> ObjectMeta | None:
        result = self.store.head(self._key(key))
        if result is None:
            return None
        return ObjectMeta(key, result.size, result.version)

    def put(self, key: str, body: bytes, *, mode: Create | Update) -> ObjectMeta:
        result = self.store.put(self._key(key), body, mode=mode)
        return ObjectMeta(key, result.size, result.version)

    def delete(self, key: str, *, if_version: Version | None = None) -> None:
        self.store.delete(self._key(key), if_version=if_version)

    def put_immutable(self, key: str, body: bytes) -> ObjectMeta:
        """Create an immutable value, accepting only byte-identical retries."""
        for attempt in range(3):
            try:
                return self.put(key, body, mode=Create())
            except PreconditionFailed:
                try:
                    current = self.get(key).body
                except ObjectNotFound as error:
                    raise ForgeError("immutable object disappeared during verification", "integrity") from error
                if current != body:
                    raise ForgeError("immutable object key contains different bytes", "integrity")
                return self.head(key) or ObjectMeta(key, len(body), Version(sha256(body)))
            except AmbiguousWrite:
                try:
                    current = self.get(key).body
                except ObjectNotFound:
                    if attempt < 2:
                        continue
                    raise ForgeError("immutable object upload outcome is unknown", "storage")
                if current != body:
                    raise ForgeError("ambiguous immutable upload stored different bytes", "integrity")
                return self.head(key) or ObjectMeta(key, len(body), Version(sha256(body)))
        raise ForgeError("immutable object upload did not complete", "storage")

    def put_descriptor(self, descriptor: dict) -> str:
        descriptor = validate_descriptor(descriptor)
        identifier = descriptor["id"]
        self.put_immutable(f"descriptors/{identifier}.json", canonical_json(descriptor))
        return identifier

    def _descriptor(self, identifier: str) -> dict:
        key = f"descriptors/{identifier}.json"
        try:
            body = self.get(key).body
            value = json.loads(body)
        except (ObjectNotFound, json.JSONDecodeError) as error:
            raise ForgeError(f"referenced descriptor is unreadable: {identifier}", "integrity") from error
        if canonical_json(value) != body:
            raise ForgeError("descriptor object is not canonical JSON", "integrity")
        descriptor = validate_descriptor(value)
        if descriptor["id"] != identifier:
            raise ForgeError("descriptor object ID differs from its key", "integrity")
        return descriptor

    def load(self) -> tuple[Manifest, Version, ReplayState]:
        """Read one manifest and its complete linked WAL chain without LIST."""
        try:
            fetched = self.get("manifest.json")
        except ObjectNotFound as error:
            raise ForgeError("repository manifest is missing", "integrity") from error
        try:
            raw_manifest = json.loads(fetched.body)
        except json.JSONDecodeError as error:
            raise ForgeError("repository manifest is malformed", "integrity") from error
        if canonical_json(raw_manifest) != fetched.body:
            raise ForgeError("repository manifest is not canonical JSON", "integrity")
        manifest = Manifest.from_dict(raw_manifest)
        if manifest.repository_id != self.repository_id:
            raise ForgeError("manifest repository ID differs from repository configuration", "integrity")
        pointer = manifest.wal_head.to_dict()
        reversed_records: list[tuple[dict, dict]] = []
        expected_seq = manifest.head_seq
        while True:
            if pointer["seq"] != expected_seq:
                raise ForgeError("WAL sequence is not contiguous", "integrity")
            try:
                fetched_record = self.get(pointer["key"])
            except ObjectNotFound as error:
                raise ForgeError("manifest references a missing WAL record", "integrity") from error
            if sha256(fetched_record.body) != pointer["sha256"]:
                raise ForgeError("WAL record hash differs from its pointer", "integrity")
            try:
                raw_record = json.loads(fetched_record.body)
            except json.JSONDecodeError as error:
                raise ForgeError("WAL record is malformed", "integrity") from error
            if canonical_json(raw_record) != fetched_record.body:
                raise ForgeError("WAL record is not canonical JSON", "integrity")
            record = validate_record(raw_record, repository_id=self.repository_id)
            if (
                record["seq"] != pointer["seq"]
                or record["event_id"] != pointer.get("event_id", record["event_id"])
                or record["publication_id"] != pointer.get("publication_id", record["publication_id"])
            ):
                raise ForgeError("WAL record differs from its manifest pointer", "integrity")
            reference = {
                "seq": record["seq"],
                "key": pointer["key"],
                "sha256": pointer["sha256"],
                "event_id": record["event_id"],
                "publication_id": record["publication_id"],
            }
            reversed_records.append((record, reference))
            if record["seq"] == 1:
                break
            previous = record["previous"]
            assert isinstance(previous, dict)
            pointer = previous
            expected_seq -= 1
        records = list(reversed(reversed_records))
        state = ReplayState()
        for record, reference in records:
            apply_record(state, record, record_ref=reference, descriptor_loader=self._descriptor)
        if (
            state.head_seq != manifest.head_seq
            or state.canonical_ref != manifest.canonical_ref
            or state.canonical_tip != manifest.canonical_tip
            or state.canonical_packs != list(manifest.canonical_packs)
            or state.head_record != manifest.wal_head.to_dict()
        ):
            raise ForgeError("manifest does not equal its replayed WAL state", "integrity")
        return manifest, fetched.meta.version, state

    def _record(
        self,
        seq: int,
        fingerprint: dict,
        payload: dict,
        previous: dict | None,
        publication_id: str,
    ) -> dict:
        identifier = event_id(fingerprint)
        return {
            "seq": seq,
            "kind": fingerprint["kind"],
            "event_fingerprint": copy.deepcopy(fingerprint),
            "event_id": identifier,
            "publication_id": publication_id,
            "previous": previous,
            "created_at": int(time.time()),
            "writer": self.writer,
            "payload": copy.deepcopy(payload),
        }

    def _run_prerequisites(self, candidate: Candidate) -> None:
        for prerequisite in candidate.prerequisites:
            prerequisite(self)

    def _manifest_for(self, state: ReplayState, previous: Manifest, writer: str) -> Manifest:
        if state.head_record is None or state.canonical_ref is None or state.canonical_tip is None:
            raise ForgeError("candidate did not produce repository state", "integrity")
        head = state.head_record
        return Manifest(
            self.repository_id,
            state.canonical_ref,
            state.canonical_tip,
            state.head_seq,
            # State head refs originate from the immutable record we just wrote.
            type(previous.wal_head)(
                head["seq"], head["key"], head["sha256"], head["event_id"], head["publication_id"]
            ),
            tuple(copy.deepcopy(state.canonical_packs)),
            previous.revision + 1,
            int(time.time()),
            writer,
        )

    def _resolve_ambiguous(self, old_head: int, publication_id: str) -> int | None:
        """Return a known sequence, or None when this attempt lost the CAS."""
        manifest, _version, _state = self.load()
        pointer = manifest.wal_head.to_dict()
        while pointer["seq"] > old_head:
            record_body = self.get(pointer["key"]).body
            record = validate_record(json.loads(record_body), repository_id=self.repository_id)
            if record["publication_id"] == publication_id:
                return record["seq"]
            if record["seq"] == 1:
                break
            previous = record["previous"]
            assert isinstance(previous, dict)
            pointer = previous
        return None

    def publish(
        self, build_event: Callable[[Manifest, ReplayState], Candidate | AlreadyCommitted]
    ) -> int:
        """Publish exactly one event through the manifest CAS linearization point."""
        last_conflict = False
        for _attempt in range(3):
            manifest, version, state = self.load()
            candidate = build_event(manifest, state)
            if isinstance(candidate, AlreadyCommitted):
                return candidate.seq
            if not isinstance(candidate, Candidate):
                raise ForgeError("WAL event builder returned an invalid candidate", "integrity")
            identifier = candidate.event_id
            existing = state.events.get(identifier)
            if existing is not None:
                if existing["event_fingerprint"] != candidate.event_fingerprint:
                    raise ForgeError("WAL event ID collides with different content", "integrity")
                return existing["seq"]
            publication_id = uuid.uuid4().hex
            record_key = f"wal/records/{manifest.head_seq + 1:020d}-{identifier}-{publication_id}.json"
            record = self._record(
                manifest.head_seq + 1,
                candidate.event_fingerprint,
                candidate.payload,
                {
                    "seq": manifest.wal_head.seq,
                    "key": manifest.wal_head.key,
                    "sha256": manifest.wal_head.sha256,
                },
                publication_id,
            )
            try:
                # Structural validation happens before uploads; validation that
                # reads descriptor objects happens after their prerequisite.
                validate_record(record, repository_id=self.repository_id)
                self._run_prerequisites(candidate)
                record_body = canonical_json(record)
                record_sha = sha256(record_body)
                self.put_immutable(record_key, record_body)
                from failpoints import checkpoint

                checkpoint(self.root, "wal.record_uploaded")
                record_ref = {
                    "seq": record["seq"],
                    "key": record_key,
                    "sha256": record_sha,
                    "event_id": identifier,
                    "publication_id": publication_id,
                }
                next_state = state.clone()
                apply_record(
                    next_state,
                    record,
                    record_ref=record_ref,
                    descriptor_loader=self._descriptor,
                )
                next_manifest = self._manifest_for(next_state, manifest, self.writer)
                try:
                    checkpoint(self.root, "wal.manifest_cas_sending")
                    self.put("manifest.json", canonical_json(next_manifest.to_dict()), mode=Update(version))
                    checkpoint(self.root, "wal.manifest_cas_committed")
                    return record["seq"]
                except PreconditionFailed:
                    last_conflict = True
                    continue
                except AmbiguousWrite:
                    try:
                        resolved = self._resolve_ambiguous(manifest.head_seq, publication_id)
                    except Exception as error:
                        raise ForgeError("manifest update outcome is unknown; retry safely", "storage") from error
                    if resolved is not None:
                        return resolved
                    last_conflict = True
                    continue
            finally:
                if candidate.cleanup is not None:
                    candidate.cleanup()
        if last_conflict:
            raise ForgeError("repository changed concurrently; fetch and retry", "conflict")
        raise ForgeError("repository publication did not complete", "storage")

    def bootstrap(self, candidate: Candidate) -> int:
        """Create a sequence-one import chain and its initial manifest."""
        try:
            manifest, _version, state = self.load()
        except ForgeError as error:
            if "manifest is missing" not in str(error):
                raise
        else:
            existing = state.events.get(candidate.event_id)
            if existing and existing["event_fingerprint"] == candidate.event_fingerprint:
                return existing["seq"]
            raise ForgeError("repository manifest already exists", "conflict")
        publication_id = uuid.uuid4().hex
        record_key = f"wal/records/{1:020d}-{candidate.event_id}-{publication_id}.json"
        record = self._record(1, candidate.event_fingerprint, candidate.payload, None, publication_id)
        try:
            validate_record(record, repository_id=self.repository_id)
            self._run_prerequisites(candidate)
            body = canonical_json(record)
            record_sha = sha256(body)
            self.put_immutable(record_key, body)
            from failpoints import checkpoint

            checkpoint(self.root, "wal.record_uploaded")
            state = ReplayState()
            reference = {
                "seq": 1,
                "key": record_key,
                "sha256": record_sha,
                "event_id": candidate.event_id,
                "publication_id": publication_id,
            }
            apply_record(state, record, record_ref=reference, descriptor_loader=self._descriptor)
            if state.canonical_ref is None or state.canonical_tip is None:
                raise ForgeError("bootstrap import did not create canonical state", "integrity")
            manifest = Manifest(
                self.repository_id,
                state.canonical_ref,
                state.canonical_tip,
                1,
                WalHead(**reference),
                tuple(state.canonical_packs),
                1,
                int(time.time()),
                self.writer,
            )
            try:
                checkpoint(self.root, "wal.manifest_cas_sending")
                self.put("manifest.json", canonical_json(manifest.to_dict()), mode=Create())
                checkpoint(self.root, "wal.manifest_cas_committed")
            except PreconditionFailed:
                loaded, _version, replay = self.load()
                existing = replay.events.get(candidate.event_id)
                if existing and existing["event_fingerprint"] == candidate.event_fingerprint:
                    return existing["seq"]
                raise ForgeError("repository bootstrap raced another manifest", "conflict")
            return 1
        finally:
            if candidate.cleanup is not None:
                candidate.cleanup()
