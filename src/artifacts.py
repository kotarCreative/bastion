"""Immutable Git pack construction, verification, upload, and installation."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from errors import ForgeError
from git import git, run
from object_store import AmbiguousWrite, Create, ObjectNotFound, PreconditionFailed
from object_store_s3 import MAX_OBJECT_SIZE
from wal import _pack_ref, sha256


@dataclass(frozen=True)
class BuiltPack:
    directory: Path
    pack_path: Path
    idx_path: Path
    pack_sha256: str
    pack_git_checksum: str
    pack_size: int
    idx_sha256: str
    idx_size: int
    object_count: int

    def reference(self, prefix: str) -> dict:
        kind = {
            "packs/canonical": "canonical",
            "packs/submissions": "submission",
        }.get(prefix)
        if kind is None:
            raise ForgeError("unknown immutable pack prefix", "integrity")
        return {
            "kind": kind,
            "pack_key": f"{prefix}/{self.pack_sha256}.pack",
            "pack_sha256": self.pack_sha256,
            "pack_git_checksum": self.pack_git_checksum,
            "pack_size": self.pack_size,
            "idx_key": f"{prefix}/{self.pack_sha256}.idx",
            "idx_sha256": self.idx_sha256,
            "idx_size": self.idx_size,
            "object_count": self.object_count,
        }

    def cleanup(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def _build(repo: Path, revisions: bytes, env: dict[str, str] | None) -> BuiltPack:
    # Git creates pack-objects output in the repository's object filesystem
    # before renaming it to the requested prefix. Keep the destination on
    # that same filesystem so deployments with a separate /tmp mount do not
    # fail with EXDEV during the final rename.
    directory = Path(tempfile.mkdtemp(prefix="bastion-pack-", dir=repo.parent))
    prefix = directory / "pack"
    try:
        produced = git(
            repo,
            "pack-objects",
            "--revs",
            str(prefix),
            data=revisions,
            env=env,
        ).stdout.decode().strip()
        if len(produced) != 40 or any(char not in "0123456789abcdef" for char in produced):
            raise ForgeError("git pack-objects returned an invalid pack checksum", "integrity")
        pack_path = directory / f"pack-{produced}.pack"
        if not pack_path.is_file():
            raise ForgeError("git pack-objects did not create a pack", "integrity")
        run("git", "index-pack", str(pack_path))
        idx_path = directory / f"pack-{produced}.idx"
        if not idx_path.is_file():
            raise ForgeError("git index-pack did not create an index", "integrity")
        if pack_path.stat().st_size > MAX_OBJECT_SIZE or idx_path.stat().st_size > MAX_OBJECT_SIZE:
            raise ForgeError("immutable pack exceeds the qualified object size limit", "integrity")
        pack_bytes = pack_path.read_bytes()
        idx_bytes = idx_path.read_bytes()
        if len(pack_bytes) < 20 or pack_bytes[-20:].hex() != produced:
            raise ForgeError("pack trailer differs from its Git checksum", "integrity")
        listing = run("git", "verify-pack", "-v", str(idx_path)).stdout.decode(
            errors="replace"
        )
        object_count = sum(
            1
            for line in listing.splitlines()
            if len(line.split()) >= 2 and line.split()[1] in {"commit", "tree", "blob", "tag"}
        )
        if object_count == 0:
            raise ForgeError("immutable pack is empty", "integrity")
        for candidate in (pack_path, idx_path):
            with candidate.open("rb") as source:
                os.fsync(source.fileno())
        return BuiltPack(
            directory,
            pack_path,
            idx_path,
            sha256(pack_bytes),
            produced,
            len(pack_bytes),
            sha256(idx_bytes),
            len(idx_bytes),
            object_count,
        )
    except Exception:
        shutil.rmtree(directory, ignore_errors=True)
        raise


def _range(old: str, new: str) -> bytes:
    if old == "0" * 40:
        return f"{new}\n".encode("ascii")
    return f"{new}\n^{old}\n".encode("ascii")


def build_canonical_pack(repo: Path, old: str, new: str, env: dict[str, str]) -> BuiltPack:
    return _build(repo, _range(old, new), env)


def build_submission_pack(repo: Path, old: str, new: str, env: dict[str, str]) -> BuiltPack:
    return _build(repo, _range(old, new), env)


def build_full_ref_pack(repo: Path, ref_name: str) -> BuiltPack:
    return _build(repo, f"{ref_name}\n".encode("ascii"), None)


def build_full_refs_pack(repo: Path, refs: list[str]) -> BuiltPack | None:
    """Build one full reachable-union pack for the supplied refs."""
    if not refs:
        return None
    revisions = ("".join(f"{ref}\n" for ref in sorted(refs))).encode("ascii")
    return _build(repo, revisions, None)


def _put_immutable(store, key: str, body: bytes) -> None:
    """Use Repository's stricter helper when available, otherwise emulate it."""
    if hasattr(store, "put_immutable"):
        store.put_immutable(key, body)
        return
    for attempt in range(3):
        try:
            store.put(key, body, mode=Create())
            return
        except PreconditionFailed:
            try:
                existing = store.get(key).body
            except ObjectNotFound as error:
                raise ForgeError("immutable pack vanished during upload", "integrity") from error
            if existing != body:
                raise ForgeError("immutable pack key has different bytes", "integrity")
            return
        except AmbiguousWrite:
            try:
                existing = store.get(key).body
            except ObjectNotFound:
                if attempt < 2:
                    continue
                raise ForgeError("immutable pack upload outcome is unknown", "storage")
            if existing != body:
                raise ForgeError("ambiguous pack upload has different bytes", "integrity")
            return
    raise ForgeError("immutable pack upload did not complete", "storage")


def upload_pack(store, prefix: str, built_pack: BuiltPack) -> dict:
    reference = built_pack.reference(prefix)
    _put_immutable(store, reference["pack_key"], built_pack.pack_path.read_bytes())
    _put_immutable(store, reference["idx_key"], built_pack.idx_path.read_bytes())
    return reference


def verify_pack_files(pack_path: Path, idx_path: Path, expected_ref: dict) -> None:
    expected = _pack_ref(expected_ref)
    try:
        pack_bytes = pack_path.read_bytes()
        idx_bytes = idx_path.read_bytes()
    except OSError as error:
        raise ForgeError("pack files are missing", "integrity") from error
    if (
        sha256(pack_bytes) != expected["pack_sha256"]
        or len(pack_bytes) != expected["pack_size"]
        or sha256(idx_bytes) != expected["idx_sha256"]
        or len(idx_bytes) != expected["idx_size"]
        or len(pack_bytes) < 20
        or pack_bytes[-20:].hex() != expected["pack_git_checksum"]
    ):
        raise ForgeError("pack bytes do not match their immutable reference", "integrity")
    listing = run("git", "verify-pack", "-v", str(idx_path)).stdout.decode(errors="replace")
    count = sum(
        1
        for line in listing.splitlines()
        if len(line.split()) >= 2 and line.split()[1] in {"commit", "tree", "blob", "tag"}
    )
    if count != expected["object_count"]:
        raise ForgeError("pack object count differs from its immutable reference", "integrity")


def install_pack(store, pack_ref: dict, destination_objects: Path) -> None:
    reference = _pack_ref(pack_ref)
    try:
        pack_bytes = store.get(reference["pack_key"]).body
        idx_bytes = store.get(reference["idx_key"]).body
    except ObjectNotFound as error:
        raise ForgeError("referenced immutable pack is missing", "integrity") from error
    if sha256(pack_bytes) != reference["pack_sha256"] or sha256(idx_bytes) != reference["idx_sha256"]:
        raise ForgeError("referenced immutable pack hash differs", "integrity")
    target = destination_objects / "pack"
    target.mkdir(parents=True, exist_ok=True)
    pack_path = target / f"pack-{reference['pack_git_checksum']}.pack"
    idx_path = target / f"pack-{reference['pack_git_checksum']}.idx"
    for path, body in ((pack_path, pack_bytes), (idx_path, idx_bytes)):
        if path.exists():
            continue
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=target)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    verify_pack_files(pack_path, idx_path, reference)
