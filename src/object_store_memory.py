"""Thread-safe in-memory implementation of :mod:`object_store`."""

from __future__ import annotations

import threading

from object_store import (
    AmbiguousWrite,
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


class MemoryObjectStore:
    """A deterministic conditional store suitable for unit tests.

    Versions deliberately use a monotonically increasing opaque counter, not
    content hashes, so a delete/recreate cycle cannot accidentally satisfy an
    old conditional request.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._objects: dict[str, tuple[bytes, Version]] = {}
        self._next_version = 1
        self.list_calls = 0

    def _version(self) -> Version:
        result = Version(str(self._next_version))
        self._next_version += 1
        return result

    def get(
        self,
        key: str,
        *,
        if_none_match: Version | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> GetResult:
        validate_key(key)
        with self._lock:
            try:
                body, version = self._objects[key]
            except KeyError as error:
                raise ObjectNotFound(key) from error
            if if_none_match == version:
                raise NotModified(key)
            if byte_range is not None:
                start, end = byte_range
                if not 0 <= start <= end < len(body):
                    raise ValueError("invalid inclusive object byte range")
                body = body[start : end + 1]
            return GetResult(body, ObjectMeta(key, len(self._objects[key][0]), version))

    def head(self, key: str) -> ObjectMeta | None:
        validate_key(key)
        with self._lock:
            current = self._objects.get(key)
            if current is None:
                return None
            body, version = current
            return ObjectMeta(key, len(body), version)

    def put(self, key: str, body: bytes, *, mode: Create | Update) -> ObjectMeta:
        validate_key(key)
        if not isinstance(body, bytes):
            raise TypeError("object bodies must be bytes")
        with self._lock:
            current = self._objects.get(key)
            if isinstance(mode, Create):
                if current is not None:
                    raise PreconditionFailed(key)
            elif isinstance(mode, Update):
                if current is None or current[1] != mode.version:
                    raise PreconditionFailed(key)
            else:
                raise TypeError("object-store puts require Create or Update")
            version = self._version()
            self._objects[key] = (body, version)
            return ObjectMeta(key, len(body), version)

    def delete(self, key: str, *, if_version: Version | None = None) -> None:
        validate_key(key)
        with self._lock:
            current = self._objects.get(key)
            if current is None:
                if if_version is not None:
                    raise PreconditionFailed(key)
                return
            if if_version is not None and current[1] != if_version:
                raise PreconditionFailed(key)
            del self._objects[key]

    def list_prefix(self, prefix: str) -> list[ObjectMeta]:
        if not isinstance(prefix, str) or not prefix.isascii():
            raise ValueError("object-store prefix must be ASCII")
        with self._lock:
            self.list_calls += 1
            return [
                ObjectMeta(key, len(body), version)
                for key, (body, version) in sorted(self._objects.items())
                if key.startswith(prefix)
            ]


__all__ = ["MemoryObjectStore", "AmbiguousWrite"]
