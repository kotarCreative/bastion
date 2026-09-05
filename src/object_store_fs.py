"""Crash-safe filesystem implementation of the conditional object store."""

from __future__ import annotations

import fcntl
import hashlib
import os
import stat
import tempfile
import threading
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


# Process-global (ino, ctime_ns, mtime_ns, size) -> Version cache. Fresh
# store handles for the same root still recognize unchanged objects from
# metadata without body reads. Writers update it on every put, so a manifest
# advanced through another handle is never misreported as unmodified (its
# stat changes); same-user on-disk tampering outside the documented boundary
# is out of scope. The stamp includes inode and ctime so a concurrent
# os.replace that reuses mtime/size can never alias the old body.
_GLOBAL_VERSION_CACHE: dict[str, tuple[int, int, int, int, Version]] = {}
_GLOBAL_VERSION_LOCK = threading.Lock()


class FilesystemObjectStore:
    """Immutable/mutable conditional objects rooted at one local directory."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._locks = self.root / ".locks"
        self._locks.mkdir(exist_ok=True)
        self.list_calls = 0
        # Operation counters for bounded-hot-path verification.
        self.get_calls = 0
        self.head_calls = 0
        self.body_reads = 0

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

    def _cache_key(self, key: str) -> str:
        return f"{self.root}\x00{key}"

    def _stat_version(
        self, key: str
    ) -> tuple[Path, int, int, int, int, Version | None]:
        path = self._path(key)
        try:
            metadata = path.stat()
        except FileNotFoundError as error:
            raise ObjectNotFound(key) from error
        if path.is_symlink():
            raise ValueError("object-store object is a symlink")
        stamp = (
            int(getattr(metadata, "st_ino", 0)),
            int(getattr(metadata, "st_ctime_ns", metadata.st_mtime_ns)),
            metadata.st_mtime_ns,
            metadata.st_size,
        )
        with _GLOBAL_VERSION_LOCK:
            cached = _GLOBAL_VERSION_CACHE.get(self._cache_key(key))
        version = (
            cached[4]
            if cached is not None and (cached[0], cached[1], cached[2], cached[3]) == stamp
            else None
        )
        return path, stamp[0], stamp[1], stamp[2], stamp[3], version

    def _remember_version(
        self, key: str, ino: int, ctime_ns: int, mtime_ns: int, size: int, version: Version
    ) -> None:
        with _GLOBAL_VERSION_LOCK:
            _GLOBAL_VERSION_CACHE[self._cache_key(key)] = (ino, ctime_ns, mtime_ns, size, version)

    def _forget_version(self, key: str) -> None:
        with _GLOBAL_VERSION_LOCK:
            _GLOBAL_VERSION_CACHE.pop(self._cache_key(key), None)

    def _stamp_of(self, metadata: "os.stat_result") -> tuple[int, int, int, int]:
        return (
            int(getattr(metadata, "st_ino", 0)),
            int(getattr(metadata, "st_ctime_ns", metadata.st_mtime_ns)),
            metadata.st_mtime_ns,
            metadata.st_size,
        )

    def _read(self, key: str) -> tuple[bytes, ObjectMeta]:
        path = self._path(key)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError as error:
            raise ObjectNotFound(key) from error
        except OSError as error:
            import errno

            if error.errno == errno.ELOOP:
                raise ValueError("object-store object is a symlink") from error
            raise
        try:
            try:
                metadata = os.fstat(descriptor)
            except OSError as error:
                raise ObjectNotFound(key) from error
            if not stat.S_ISREG(metadata.st_mode):
                if stat.S_ISLNK(metadata.st_mode):
                    raise ValueError("object-store object is a symlink")
                raise ObjectNotFound(key)
            stamp = self._stamp_of(metadata)
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                # The fd is owned by the fdopen wrapper from here; prevent
                # double-close on the outer handler.
                descriptor = -1
                body = source.read()
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        self.body_reads += 1
        meta = self._meta(key, body)
        try:
            self._remember_version(key, stamp[0], stamp[1], stamp[2], stamp[3], meta.version)
        except OSError:
            pass
        return body, meta

    def get(
        self,
        key: str,
        *,
        if_none_match: Version | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> GetResult:
        self.get_calls += 1
        if if_none_match is not None and byte_range is None:
            try:
                _path, _ino, _ctime_ns, _mtime_ns, _size, remembered = self._stat_version(key)
            except ObjectNotFound:
                raise
            if remembered is not None and remembered == if_none_match:
                raise NotModified(key)
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
        self.head_calls += 1
        try:
            _path, _ino, _ctime_ns, _mtime_ns, size, remembered = self._stat_version(key)
        except ObjectNotFound:
            return None
        if remembered is not None:
            return ObjectMeta(key, size, remembered)
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
            meta = self._meta(key, body)
            try:
                metadata = path.stat()
                if not path.is_symlink():
                    stamp = self._stamp_of(metadata)
                    self._remember_version(
                        key, stamp[0], stamp[1], stamp[2], stamp[3], meta.version
                    )
            except OSError:
                pass
            return meta

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
            self._forget_version(key)
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
