"""Pure authorization and tree-transition rules."""

from __future__ import annotations

import os
import re

from errors import ForgeError
from layout import ZERO_OID


TreeEntry = tuple[bytes, bytes, str]
TreeEntries = dict[bytes, TreeEntry]
TreeChanges = dict[bytes, TreeEntry | None]

CHANGE_ID = re.compile(
    rb"^Change-Id:[ \t]*([A-Za-z0-9._:-]{3,128})[ \t]*$",
    re.MULTILINE,
)
OID = re.compile(rb"^[0-9a-f]{40}$")
ALLOWED_MODES = {b"100644", b"100755", b"120000"}
VIEW_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def validate_view_name(name: str) -> None:
    if not VIEW_NAME.fullmatch(name):
        raise ForgeError("view name must be a simple 1-64 character identifier")


def normalize_prefix(raw: str) -> str:
    value = raw.strip("/")
    if value == "." or "\\" in value or any(
        part in ("", ".", "..") for part in value.split("/")
    ):
        if raw not in ("", "/"):
            raise ForgeError(f"invalid path prefix: {raw}")
    return value


def allowed(path: bytes, prefixes: list[str]) -> bool:
    for raw in prefixes:
        prefix = os.fsencode(raw)
        if not prefix or path == prefix or path.startswith(prefix + b"/"):
            return True
    return False


def changed_entries(parent: TreeEntries, child: TreeEntries) -> TreeChanges:
    return {
        path: child.get(path)
        for path in parent.keys() | child.keys()
        if parent.get(path) != child.get(path)
    }


def authorize_changes(changes: TreeChanges, read: list[str], write: list[str]) -> None:
    for path in changes:
        if not allowed(path, write) or not allowed(path, read):
            raise ForgeError("submission changes a path outside current authorization")


def apply_changes(canonical: TreeEntries, changes: TreeChanges) -> TreeEntries:
    """Validate and apply exact snapshot effects to a canonical flat tree."""
    for path, entry in changes.items():
        for affected in (canonical.get(path), entry):
            if affected is not None and (
                affected[0] not in ALLOWED_MODES or affected[1] != b"blob"
            ):
                raise ForgeError(
                    "submitted tree contains an unsupported entry type or mode"
                )
        for existing in canonical:
            conflict = existing != path and (
                existing.startswith(path + b"/") or path.startswith(existing + b"/")
            )
            if conflict and changes.get(existing, canonical[existing]) is not None:
                raise ForgeError(
                    "tree transition crosses a file/directory authorization boundary"
                )

    result = canonical.copy()
    for path in sorted(changes):
        if changes[path] is None:
            result.pop(path, None)
    for path in sorted(changes):
        if changes[path] is not None:
            result[path] = changes[path]

    paths = sorted(result)
    for index, path in enumerate(paths):
        if index and path.startswith(paths[index - 1] + b"/"):
            raise ForgeError(
                "tree transition crosses a file/directory authorization boundary"
            )
    return result


def parse_change_id(message: bytes) -> str | None:
    trailer = message.rstrip().rsplit(b"\n\n", 1)[-1]
    matches = CHANGE_ID.findall(trailer)
    if len(matches) > 1:
        raise ForgeError("a commit may contain at most one Change-Id trailer")
    return matches[0].decode() if matches else None


def parse_updates(updates: bytes) -> list[tuple[str, str, str]]:
    parsed: list[tuple[str, str, str]] = []
    for line in updates.splitlines():
        if not line:
            continue
        fields = line.split()
        if len(fields) != 3:
            raise ForgeError("malformed ref update")
        old, new, raw_ref = fields
        if not OID.fullmatch(old) or not OID.fullmatch(new):
            raise ForgeError("malformed ref update")
        parsed.append(
            (
                old.decode("ascii"),
                new.decode("ascii"),
                raw_ref.decode(errors="surrogateescape"),
            )
        )
    return parsed


def validate_ref_update(
    view: dict, updates: list[tuple[str, str, str]]
) -> tuple[str, str, str]:
    if len(updates) != 1:
        raise ForgeError("exactly one authorized branch may be updated")
    old, new, updated_ref = updates[0]
    if updated_ref != view["projected_ref"]:
        raise ForgeError("branch update is not authorized")
    if old == ZERO_OID or new == ZERO_OID or old != view["projected_tip"]:
        raise ForgeError(
            "branch creation, deletion, and stale or force updates are not supported"
        )
    return old, new, updated_ref
