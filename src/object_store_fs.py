"""Crash-safe filesystem implementation of the conditional object store."""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from object_store import (
    Create,
    GetResult,
    NotModified,
    ObjectMeta,
    ObjectNotFound,
    PreconditionFailed,
    Update,
    Version,
    validate_key,
)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class FilesystemObjectStore:
    """Immutable/mutable conditional objects rooted at one local directory."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks = self.root / ".locks"
        self._locks.mkdir(exist_ok=True)
        self.list_calls = 0

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = self.root.joinpath(*key.split("/"))
        # Key validation prevents traversal. Refuse a pre-existing symlink in
        # any parent so an untrusted on-disk object cannot redirect a request.
        cursor = self.root
        for component in key.split("/")[:-1]:
            cursor = cursor / component
            if cursor.is_symlink():
                raise ValueError("object-store path has a symlink parent")
        return path

    def _lock_path(self, key: str) -> Path:
        return self._locks / hashlib.sha256(key.encode("ascii")).hexdigest()

    @contextmanager
    def _locked(self, key: str):
        lock_path = self._lock_path(key)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @staticmethod
    def _meta(key: str, body: bytes) -> ObjectMeta:
        return ObjectMeta(
            key,
            len(body),
            Version(hashlib.sha256(body).hexdigest()),
        )

    def _read(self, key: str) -> tuple[bytes, ObjectMeta]:
        path = self._path(key)
        try:
            with path.open("rb") as source:
                body = source.read()
        except FileNotFoundError as error:
            raise ObjectNotFound(key) from error
        if path.is_symlink():
            raise ValueError("object-store object is a symlink")
        return body, self._meta(key, body)

    def get(
        self,
        key: str,
        *,
        if_none_match: Version | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> GetResult:
        body, meta = self._read(key)
        if if_none_match == meta.version:
            raise NotModified(key)
        if byte_range is not None:
            start, end = byte_range
            if not 0 <= start <= end < len(body):
                raise ValueError("invalid inclusive object byte range")
            body = body[start : end + 1]
        return GetResult(body, meta)

    def head(self, key: str) -> ObjectMeta | None:
        try:
            _body, meta = self._read(key)
        except ObjectNotFound:
            return None
        return meta

    def put(self, key: str, body: bytes, *, mode: Create | Update) -> ObjectMeta:
        if not isinstance(body, bytes):
            raise TypeError("object bodies must be bytes")
        path = self._path(key)
        with self._locked(key):
            try:
                current, current_meta = self._read(key)
            except ObjectNotFound:
                current = None
                current_meta = None
            if isinstance(mode, Create):
                if current is not None:
                    raise PreconditionFailed(key)
            elif isinstance(mode, Update):
                if current_meta is None or current_meta.version != mode.version:
                    raise PreconditionFailed(key)
            else:
                raise TypeError("object-store puts require Create or Update")
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", dir=path.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as target:
                    target.write(body)
                    target.flush()
                    os.fsync(target.fileno())
                os.replace(temporary, path)
                _fsync_directory(path.parent)
            finally:
                temporary.unlink(missing_ok=True)
            return self._meta(key, body)

    def delete(self, key: str, *, if_version: Version | None = None) -> None:
        path = self._path(key)
        with self._locked(key):
            try:
                body, meta = self._read(key)
            except ObjectNotFound:
                if if_version is not None:
                    raise PreconditionFailed(key)
                return
            if if_version is not None and meta.version != if_version:
                raise PreconditionFailed(key)
            path.unlink()
            _fsync_directory(path.parent)

    def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        if not isinstance(prefix, str) or not prefix.isascii():
            raise ValueError("object-store prefix must be ASCII")
        self.list_calls += 1
        result: list[ObjectMeta] = []
        for candidate in self.root.rglob("*"):
            if not candidate.is_file() or self._locks in candidate.parents:
                continue
            key = candidate.relative_to(self.root).as_posix()
            if key.startswith(prefix):
                body = candidate.read_bytes()
                result.append(self._meta(key, body))
        return sorted(result, key=lambda meta: meta.key)
