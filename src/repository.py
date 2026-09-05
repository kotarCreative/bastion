"""Manifest-backed repository reads and the single WAL publication protocol."""

from __future__ import annotations

import copy
import json
import os
import threading
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
    NotModified,
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


# Process-local incremental snapshot cache: at most the latest validated
# snapshot per repository/configuration identity. Contents are disposable
# derived state, never persisted as repository truth, and never used by
# ``forge.py check`` as evidence.
_SNAPSHOT_CACHE: dict[str, tuple[Manifest, Version, ReplayState]] = {}
_SNAPSHOT_LOCK = threading.Lock()
# Immutable descriptor cache keyed by (snapshot identity, descriptor ID).
# Two restored roots sharing a repository ID establish descriptor presence
# independently because the identity includes the normalized root/store.
_DESCRIPTOR_CACHE: dict[tuple[str, str], dict] = {}


def _cache_enabled() -> bool:
    return os.environ.get("BASTION_REPO_CACHE", "1") != "0"


# Deterministic operation counters for bounded-hot-path verification.
_LOAD_COUNTS = {
    "full_loads": 0,
    "suffix_advances": 0,
    "conditional_hits": 0,
    "descriptor_fetches": 0,
}


def repository_load_counts() -> dict[str, int]:
    return dict(_LOAD_COUNTS)


def reset_repository_load_counts() -> None:
    for key in _LOAD_COUNTS:
        _LOAD_COUNTS[key] = 0


def clear_repository_cache() -> None:
    """Evict all cached snapshots and descriptors (tests, process reset)."""
    with _SNAPSHOT_LOCK:
        _SNAPSHOT_CACHE.clear()
        _DESCRIPTOR_CACHE.clear()


def _evict_identity(identity: str) -> None:
    """Drop one identity's snapshot and descriptor presence.

    Any mutation through a ``Repository`` handle may contradict previously
    validated reads, so writers evict before readers can serve them stale.
    Pure readers (no puts/deletes) keep their zero-read fast path.
    """
    with _SNAPSHOT_LOCK:
        _SNAPSHOT_CACHE.pop(identity, None)
        for cached_key in [key for key in _DESCRIPTOR_CACHE if key[0] == identity]:
            del _DESCRIPTOR_CACHE[cached_key]


def _snapshot_identity(repository: "Repository") -> str:
    store = repository.store
    backing = getattr(store, "root", None)
    if backing is not None:
        try:
            store_id = str(Path(backing).resolve())
        except OSError:
            store_id = str(backing)
    else:
        store_id = f"memory:{id(store)}"
    try:
        root_id = str(repository.root.resolve())
    except OSError:
        root_id = str(repository.root)
    return f"{root_id}\x00{repository.repository_id}\x00{store_id}"


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
        _evict_identity(_snapshot_identity(self))
        return ObjectMeta(key, result.size, result.version)

    def delete(self, key: str, *, if_version: Version | None = None) -> None:
        self.store.delete(self._key(key), if_version=if_version)
        _evict_identity(_snapshot_identity(self))

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

    def _memoized_descriptor_loader(self, identity: str) -> Callable[[str], dict]:
        """Fetch and canonically validate each descriptor ID at most once.

        Entries are immutable and keyed by (repository identity, ID) so two
        restored roots sharing a repository ID establish presence
        independently.
        """
        local: dict[str, dict] = {}

        def load(identifier: str) -> dict:
            hit = local.get(identifier)
            if hit is not None:
                return hit
            cache_key = (identity, identifier)
            with _SNAPSHOT_LOCK:
                remembered = _DESCRIPTOR_CACHE.get(cache_key)
            if remembered is not None:
                local[identifier] = remembered
                return remembered
            _LOAD_COUNTS["descriptor_fetches"] += 1
            value = self._descriptor(identifier)
            with _SNAPSHOT_LOCK:
                _DESCRIPTOR_CACHE[cache_key] = value
            local[identifier] = value
            return value

        return load

    def load(self, *, use_cache: bool = True) -> tuple[Manifest, Version, ReplayState]:
        """Read one manifest snapshot, incrementally when a snapshot is cached.

        An unchanged load performs one conditional ``manifest.json`` GET using
        the cached object-store version; ``NotModified`` returns a defensive
        copy of the cached snapshot without historical WAL or descriptor
        reads. The cached entry itself is never exposed mutably: every return
        clones the replay state so callers cannot corrupt subsequent loads.
        When the manifest advances, loading walks only the new suffix back to
        the cached head. Cache eviction, process restart, or disabled caching
        falls back to complete replay. Writers continue to use the exact
        manifest version from their snapshot for CAS, so a process-local
        cache cannot make a stale write commit.
        """
        if not use_cache or not _cache_enabled():
            return self._load_full()
        identity = _snapshot_identity(self)
        with _SNAPSHOT_LOCK:
            cached = _SNAPSHOT_CACHE.get(identity)
        if cached is None:
            result = self._load_full()
            with _SNAPSHOT_LOCK:
                _SNAPSHOT_CACHE[identity] = result
            manifest, version, replay = result
            return manifest, version, replay.clone()
        manifest, version, replay = cached
        try:
            fetched = self.get("manifest.json", if_none_match=version)
        except NotModified:
            _LOAD_COUNTS["conditional_hits"] += 1
            return manifest, version, replay.clone()
        except ObjectNotFound as error:
            raise ForgeError("repository manifest is missing", "integrity") from error
        _LOAD_COUNTS["suffix_advances"] += 1
        result = self._load_suffix(identity, manifest, replay, fetched)
        with _SNAPSHOT_LOCK:
            _SNAPSHOT_CACHE[identity] = result
        manifest, version, replay = result
        return manifest, version, replay.clone()

    def _install_snapshot(
        self, manifest: Manifest, version: Version, replay: ReplayState
    ) -> None:
        """Install a newly validated snapshot after a successful local CAS."""
        if not _cache_enabled():
            return
        with _SNAPSHOT_LOCK:
            _SNAPSHOT_CACHE[_snapshot_identity(self)] = (manifest, version, replay)

    def _parse_manifest(self, body: bytes) -> Manifest:
        try:
            raw_manifest = json.loads(body)
        except json.JSONDecodeError as error:
            raise ForgeError("repository manifest is malformed", "integrity") from error
        if canonical_json(raw_manifest) != body:
            raise ForgeError("repository manifest is not canonical JSON", "integrity")
        manifest = Manifest.from_dict(raw_manifest)
        if manifest.repository_id != self.repository_id:
            raise ForgeError("manifest repository ID differs from repository configuration", "integrity")
        return manifest

    def _fetch_record(self, pointer: dict, expected_seq: int) -> tuple[dict, dict]:
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
        return record, reference

    def _check_replay_matches(self, manifest: Manifest, state: ReplayState) -> None:
        if (
            state.head_seq != manifest.head_seq
            or state.canonical_ref != manifest.canonical_ref
            or state.canonical_tip != manifest.canonical_tip
            or state.canonical_packs != list(manifest.canonical_packs)
            or state.head_record != manifest.wal_head.to_dict()
        ):
            raise ForgeError("manifest does not equal its replayed WAL state", "integrity")

    def _load_full(self) -> tuple[Manifest, Version, ReplayState]:
        """Read one manifest and its complete linked WAL chain without LIST."""
        _LOAD_COUNTS["full_loads"] += 1
        try:
            fetched = self.get("manifest.json")
        except ObjectNotFound as error:
            raise ForgeError("repository manifest is missing", "integrity") from error
        manifest = self._parse_manifest(fetched.body)
        loader = self._memoized_descriptor_loader(_snapshot_identity(self))
        pointer = manifest.wal_head.to_dict()
        reversed_records: list[tuple[dict, dict]] = []
        expected_seq = manifest.head_seq
        while True:
            record, reference = self._fetch_record(pointer, expected_seq)
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
            apply_record(state, record, record_ref=reference, descriptor_loader=loader)
        self._check_replay_matches(manifest, state)
        return manifest, fetched.meta.version, state

    def _load_suffix(
        self,
        identity: str,
        cached_manifest: Manifest,
        cached_replay: ReplayState,
        fetched: GetResult,
    ) -> tuple[Manifest, Version, ReplayState]:
        """Advance a cached snapshot across one manifest generation change."""
        manifest = self._parse_manifest(fetched.body)
        if manifest.head_seq < cached_manifest.head_seq:
            raise ForgeError("repository manifest moved backwards", "integrity")
        cached_head = cached_manifest.wal_head.to_dict()
        if manifest.head_seq == cached_manifest.head_seq:
            if manifest.wal_head.to_dict() != cached_head:
                raise ForgeError("repository manifest head changed at the same sequence", "integrity")
            # Same-sequence generations must still describe the same replayed
            # WAL state; canonical_tip/ref/packs are repository truth and are
            # validated rather than trusted from the new manifest bytes.
            self._check_replay_matches(manifest, cached_replay)
            return manifest, fetched.meta.version, cached_replay
        loader = self._memoized_descriptor_loader(identity)
        pointer = manifest.wal_head.to_dict()
        expected_seq = manifest.head_seq
        suffix: list[tuple[dict, dict]] = []
        while pointer["seq"] > cached_manifest.head_seq:
            record, reference = self._fetch_record(pointer, expected_seq)
            suffix.append((record, reference))
            previous = record["previous"]
            assert isinstance(previous, dict)
            pointer = previous
            expected_seq -= 1
        # The first suffix record's exact previous sequence, key, and hash
        # must identify the cached head; the cached manifest head separately
        # binds its event and publication IDs.
        if (
            pointer.get("seq") != cached_head["seq"]
            or pointer.get("key") != cached_head["key"]
            or pointer.get("sha256") != cached_head["sha256"]
        ):
            raise ForgeError("WAL suffix does not reach the cached head", "integrity")
        if (
            cached_replay.head_record is None
            or cached_replay.head_record.get("event_id") != cached_head["event_id"]
            or cached_replay.head_record.get("publication_id") != cached_head["publication_id"]
        ):
            raise ForgeError("cached manifest head binding is inconsistent", "integrity")
        state = cached_replay.clone_for_append()
        for record, reference in reversed(suffix):
            apply_record(state, record, record_ref=reference, descriptor_loader=loader)
        self._check_replay_matches(manifest, state)
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
                # Publication derives a private copy-on-write candidate
                # state; it does not deep-copy immutable historical WAL
                # payloads merely to append one record.
                next_state = state.clone_for_append()
                apply_record(
                    next_state,
                    record,
                    record_ref=record_ref,
                    descriptor_loader=self._memoized_descriptor_loader(
                        _snapshot_identity(self)
                    ),
                )
                next_manifest = self._manifest_for(next_state, manifest, self.writer)
                try:
                    checkpoint(self.root, "wal.manifest_cas_sending")
                    meta = self.put("manifest.json", canonical_json(next_manifest.to_dict()), mode=Update(version))
                    checkpoint(self.root, "wal.manifest_cas_committed")
                    self._install_snapshot(next_manifest, meta.version, next_state)
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
                meta = self.put("manifest.json", canonical_json(manifest.to_dict()), mode=Create())
                checkpoint(self.root, "wal.manifest_cas_committed")
                self._install_snapshot(manifest, meta.version, state)
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
