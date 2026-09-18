"""Small conditional object-store contract used for repository truth."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Version:
    value: str


@dataclass(frozen=True)
class ObjectMeta:
    key: str
    size: int
    version: Version


@dataclass(frozen=True)
class GetResult:
    body: bytes
    meta: ObjectMeta


@dataclass(frozen=True)
class Create:
    pass


@dataclass(frozen=True)
class Update:
    version: Version


class ObjectStoreError(Exception):
    """Base class for object-store failures."""


class ObjectNotFound(ObjectStoreError):
    pass


class NotModified(ObjectStoreError):
    pass


class PreconditionFailed(ObjectStoreError):
    pass


class AmbiguousWrite(ObjectStoreError):
    """The write might have completed, but the caller cannot know."""


class StorageFailure(ObjectStoreError):
    """A definite storage failure that is not a repository precondition."""


class ObjectStore(Protocol):
    def get(
        self,
        key: str,
        *,
        if_none_match: Version | None = None,
        byte_range: tuple[int, int] | None = None,
    ) -> GetResult: ...

    def head(self, key: str) -> ObjectMeta | None: ...

    def put(self, key: str, body: bytes, *, mode: Create | Update) -> ObjectMeta: ...

    def delete(self, key: str, *, if_version: Version | None = None) -> None: ...

    def list_prefix(self, prefix: str) -> list[ObjectMeta]: ...


def validate_key(key: str) -> None:
    """Reject keys that cannot be mapped safely below a filesystem root."""
    if not isinstance(key, str) or not key or not key.isascii() or "\x00" in key:
        raise ValueError("object-store key must be a nonempty ASCII path")
    if key.startswith("/") or key.endswith("/") or "\\" in key:
        raise ValueError("object-store key is not a relative path")
    for component in key.split("/"):
        if not component or component in {".", ".."}:
            raise ValueError("object-store key has an unsafe path component")
