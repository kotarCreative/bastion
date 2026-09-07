"""Authenticated demo API ``/api/v1/*`` (spec 013).

Exact-shape, byte-safe JSON over the authorization-safe projected
repository. Every route requires ``ui`` scope. Errors use
``{"error": <stable-code>}`` and never include internal exceptions.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import subprocess
import time
from pathlib import Path

from errors import ForgeError
from git import clean_env
from git import git as git_command
from git import ref as git_ref
from git import tree_entries
from identity import CAPABILITIES
from validation import capability_allowed, longest_covering, normalize_prefix

DEFAULT_LIMIT = 50
MAX_LIMIT = 100
MAX_QUERY_BYTES = 4096
MAX_JSON_BODY_BYTES = 16 * 1024
MAX_B64_FIELD_BYTES = 8192
MAX_PATH_BYTES = 4096
MAX_CURSOR_BYTES = 512
CURSOR_TTL_SECONDS = 3600

ERROR_AUTH = "auth-required"
ERROR_FORBIDDEN = "forbidden"
ERROR_NOT_FOUND = "not-found"
ERROR_BAD_REQUEST = "bad-request"
ERROR_UNAVAILABLE = "service-unavailable"


def b64encode_raw(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64decode_field(value: str) -> bytes:
    if not isinstance(value, str) or not value.isascii():
        raise ForgeError("invalid base64 field")
    if len(value.encode("ascii")) > MAX_B64_FIELD_BYTES:
        raise ForgeError("base64 field is too large")
    padded = value + "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError) as error:
        raise ForgeError("invalid base64 field") from error


def display(raw: bytes) -> str:
    """Lossy presentation text with visible control-byte escapes."""
    text = raw.decode("utf-8", "replace")
    return "".join(
        char if char == "\n" or char == "\t" or ord(char) >= 32 else f"\\x{ord(char):02x}"
        for char in text
    )


def prefix_object(raw_prefix: str) -> dict:
    raw = os.fsencode(raw_prefix)
    return {"prefix_b64": b64encode_raw(raw), "prefix_display": display(raw)}


def capability_map_object(raw_map: dict) -> dict:
    result: dict[str, list[dict]] = {}
    for capability in CAPABILITIES:
        result[capability] = [prefix_object(item) for item in raw_map.get(capability, [])]
    return result


def normalize_api_path(raw: bytes) -> str:
    if len(raw) > MAX_PATH_BYTES:
        raise ForgeError("path is too large")
    if b"\x00" in raw:
        raise ForgeError("invalid path")
    text = raw.decode("utf-8", "surrogateescape")
    return normalize_prefix(text)


def parse_limit(value: str | None) -> int:
    if value is None or value == "":
        return DEFAULT_LIMIT
    try:
        number = int(value)
    except ValueError as error:
        raise ForgeError("invalid limit") from error
    if isinstance(number, bool) or number < 1 or number > MAX_LIMIT:
        raise ForgeError("invalid limit")
    return number


# -- cursors ---------------------------------------------------------------

def _cursor_key(secret: str) -> bytes:
    return hashlib.sha256(b"bastion-api-cursor-v1\0" + secret.encode()).digest()


def mint_cursor(
    secret: str,
    route: str,
    view: str,
    tip: str,
    prefix_b64: str,
    offset: int,
    anchor: str = "",
) -> str:
    payload = {
        "v": 1,
        "route": route,
        "view": view,
        "tip": tip,
        "prefix": prefix_b64,
        "off": offset,
        "anchor": anchor,
        "at": int(time.time()),
    }
    body = b64encode_raw(json.dumps(payload, sort_keys=True).encode())
    signature = b64encode_raw(
        hmac.new(_cursor_key(secret), body.encode(), hashlib.sha256).digest()
    )
    return f"{body}.{signature}"


def parse_cursor(
    secret: str,
    cursor: str,
    route: str,
    view: str,
    tip: str,
    prefix_b64: str,
) -> tuple[int, str]:
    """Validate one cursor and return (offset, anchor).

    The anchor is the bounded next-commit cursor for history pages: it
    names the last commit of the previous page so each page visits at most
    ``limit + 1`` commits instead of enumerating the full history.
    """
    if not isinstance(cursor, str) or len(cursor.encode()) > MAX_CURSOR_BYTES:
        raise ForgeError("invalid cursor")
    try:
        body, _, signature = cursor.partition(".")
        if not body or not signature:
            raise ForgeError("invalid cursor")
        expected = b64encode_raw(
            hmac.new(_cursor_key(secret), body.encode(), hashlib.sha256).digest()
        )
        if not hmac.compare_digest(signature, expected):
            raise ForgeError("invalid cursor")
        padded = body + "=" * (-len(body) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except (binascii.Error, ValueError, json.JSONDecodeError) as error:
        raise ForgeError("invalid cursor") from error
    if not isinstance(payload, dict) or payload.get("v") != 1:
        raise ForgeError("invalid cursor")
    if (
        payload.get("route") != route
        or payload.get("view") != view
        or payload.get("tip") != tip
        or payload.get("prefix") != prefix_b64
    ):
        raise ForgeError("invalid cursor")
    offset = payload.get("off")
    issued = payload.get("at")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ForgeError("invalid cursor")
    if not isinstance(issued, int) or time.time() - issued > CURSOR_TTL_SECONDS:
        raise ForgeError("invalid cursor")
    anchor = payload.get("anchor", "")
    if not isinstance(anchor, str) or len(anchor) > 64 or (
        anchor and any(char not in "0123456789abcdef" for char in anchor)
    ):
        raise ForgeError("invalid cursor")
    return offset, anchor


# -- projected repository access --------------------------------------------

def _materialized_view(root: Path, view: str):
    from http_server import _resolve_service

    try:
        _repository, _manifest, _active, materialized = _resolve_service(root, view)
    except ForgeError as error:
        raise ForgeError("view is unavailable") from error
    if not materialized.path.is_dir():
        raise ForgeError("view is unavailable")
    return materialized.path


def projected_tip(repo_path: Path) -> str:
    tip = git_ref(repo_path, "HEAD")
    if not tip or tip == "0" * 40:
        raise ForgeError("view is unavailable")
    return tip


def direct_children(
    repo_path: Path, descriptor: dict, normalized: str
) -> list[tuple[bytes, bytes, bytes, str]]:
    """Visible direct children of ``normalized`` sorted by raw path bytes.

    A child subtree's visibility is proven by an early-exit streamed walk
    of that child only; unrelated sibling subtrees are never enumerated.
    """
    allow = descriptor["capabilities"]["read"]
    deny = descriptor["restrictions"]["read"]
    treeish = f"HEAD:{normalized}" if normalized else "HEAD"
    try:
        children = tree_entries(repo_path, treeish, recursive=False)
    except ForgeError as error:
        raise ForgeError("no such path") from error
    prefix_b = os.fsencode(f"{normalized}/") if normalized else b""
    visible: list[tuple[bytes, bytes, bytes, str]] = []
    for name_b, (mode, kind, oid) in children.items():
        full_b = prefix_b + name_b
        if kind == b"tree":
            if not _subtree_has_visible_leaf_streamed(
                repo_path,
                descriptor,
                f"HEAD:{full_b.decode('utf-8', 'surrogateescape')}",
                full_b + b"/",
            ):
                continue
        elif not capability_allowed(allow, deny, full_b):
            continue
        visible.append((full_b, mode, kind, oid))
    visible.sort(key=lambda item: item[0])
    return visible


def _subtree_has_visible_leaf(repo_path: Path, descriptor: dict, normalized: str) -> bool:
    """Legacy whole-tree scan; prefer ``_subtree_has_visible_leaf_streamed``."""
    allow = descriptor["capabilities"]["read"]
    deny = descriptor["restrictions"]["read"]
    prefix_b = os.fsencode(f"{normalized}/") if normalized else b""
    try:
        leaves = tree_entries(repo_path, "HEAD", recursive=True)
    except ForgeError:
        return False
    return any(
        leaf_b.startswith(prefix_b) and capability_allowed(allow, deny, leaf_b)
        for leaf_b in leaves
    )


def blob_size(repo_path: Path, oid: str) -> int:
    out = git_command(repo_path, "cat-file", "-s", oid).stdout.strip()
    return int(out.decode("ascii"))


def batched_blob_sizes(repo_path: Path, oids: list[str]) -> dict[str, int]:
    """Read every blob size for one page in one cat-file process."""
    if not oids:
        return {}
    query = ("".join(f"{oid}\n" for oid in oids)).encode("ascii")
    raw = git_command(repo_path, "cat-file", "--batch-check=%(objectname) %(objectsize)", data=query).stdout
    sizes: dict[str, int] = {}
    for line in raw.splitlines():
        fields = line.split(b" ")
        if len(fields) == 2 and len(fields[0]) == 40:
            try:
                sizes[fields[0].decode("ascii")] = int(fields[1])
            except ValueError as error:
                raise ForgeError("blob size could not be read") from error
    return sizes


def _subtree_has_visible_leaf_streamed(
    repo_path: Path, descriptor: dict, treeish: str, prefix: bytes = b""
) -> bool:
    """Whether any visible leaf exists beneath ``treeish``, stopping early.

    The recursive listing streams and the check returns at the first
    visible leaf, so a visible subtree costs work proportional to its
    first leaf rather than its full descendant count.  Unrelated sibling
    subtrees are never walked.  ``ls-tree`` emits paths relative to the
    subtree, so ``prefix`` restores repository-relative byte paths.
    """
    allow = descriptor["capabilities"]["read"]
    deny = descriptor["restrictions"]["read"]
    process = subprocess.Popen(
        [
            "git",
            "--git-dir",
            os.fsencode(repo_path),
            "ls-tree",
            "-rz",
            "--name-only",
            "--full-tree",
            treeish,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=clean_env(),
    )
    try:
        pending = b""
        while True:
            block = process.stdout.read(8192)
            if not block:
                break
            pending += block
            parts = pending.split(b"\0")
            pending = parts.pop()
            for relative in parts:
                if relative and capability_allowed(allow, deny, prefix + relative):
                    return True
        if pending and capability_allowed(allow, deny, prefix + pending):
            return True
        return False
    finally:
        if process.poll() is None:
            process.kill()
        for stream in (process.stdout,):
            try:
                stream.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def history_page(
    repo_path: Path, after_oid: str | None, limit: int
) -> tuple[list[str], str | None, bool]:
    """Return the next page of projected commits visiting at most limit+1.

    ``after_oid`` is the integrity-protected cursor anchor: the page walks
    that commit's first-parent ancestors only, never the full history.
    """
    start = f"{after_oid}~1" if after_oid else "HEAD"
    raw = git_command(repo_path, "rev-list", "-n", str(limit + 1), start).stdout
    oids = [line.decode("ascii") for line in raw.split() if line]
    has_more = len(oids) > limit
    page = oids[:limit]
    last = page[-1] if page else None
    return page, last, has_more


def batched_commits(repo_path: Path, oids: list[str]) -> list[dict]:
    """Parse every commit of one page from a single cat-file process."""
    if not oids:
        return []
    query = ("".join(f"{oid}\n" for oid in oids)).encode("ascii")
    raw = git_command(repo_path, "cat-file", "--batch", data=query).stdout
    commits: list[dict] = []
    cursor = 0
    for oid in oids:
        header_end = raw.find(b"\n", cursor)
        if header_end < 0:
            raise ForgeError("commit metadata could not be read")
        fields = raw[cursor:header_end].split(b" ")
        if len(fields) != 3 or fields[0].decode("ascii", "replace") != oid:
            raise ForgeError("commit metadata could not be read")
        try:
            size = int(fields[2])
        except ValueError as error:
            raise ForgeError("commit metadata could not be read") from error
        body = raw[header_end + 1 : header_end + 1 + size]
        if len(body) != size or raw[header_end + 1 + size : header_end + 2 + size] != b"\n":
            raise ForgeError("commit metadata could not be read")
        parsed = parse_commit_object(oid, body)
        commits.append(parsed)
        cursor = header_end + 1 + size + 1
    return commits


def parse_commit_object(oid: str, raw: bytes) -> dict:
    """Parse one commit body (as delivered by cat-file --batch)."""
    header, _, message = raw.partition(b"\n\n")
    parents: list[bytes] = []
    author: bytes | None = None
    author_ts: int = 0
    author_tz: bytes = b"+0000"
    committer: bytes | None = None
    committer_ts: int = 0
    committer_tz: bytes = b"+0000"
    author_name = author_email = committer_name = committer_email = b""
    for line in header.split(b"\n"):
        if line.startswith(b"parent "):
            parents.append(line[len(b"parent "):].strip())
        elif line.startswith(b"author "):
            author = line[len(b"author "):]
        elif line.startswith(b"committer "):
            committer = line[len(b"committer "):]
    for label, value in (("author", author), ("committer", committer)):
        if value is None:
            raise ForgeError("malformed commit object")
        try:
            name_part, _, rest = value.rpartition(b" <")
            email_part, _, ts_part = rest.partition(b"> ")
            ts_text, _, tz_text = ts_part.partition(b" ")
            if label == "author":
                author_name, author_email = name_part, email_part
                author_ts, author_tz = int(ts_text), tz_text.strip() or b"+0000"
            else:
                committer_name, committer_email = name_part, email_part
                committer_ts, committer_tz = int(ts_text), tz_text.strip() or b"+0000"
        except ValueError as error:
            raise ForgeError("malformed commit object") from error
    parent_oid = parents[0].decode("ascii") if parents else None
    if parent_oid is not None and (
        len(parent_oid) != 40 or any(c not in "0123456789abcdef" for c in parent_oid)
    ):
        raise ForgeError("malformed commit object")
    return {
        "oid": oid,
        "parent_oid": parent_oid,
        "author_name": author_name,
        "author_email": author_email,
        "authored_at": author_ts,
        "authored_timezone": author_tz.decode("ascii", "replace"),
        "committer_name": committer_name,
        "committer_email": committer_email,
        "committed_at": committer_ts,
        "committed_timezone": committer_tz.decode("ascii", "replace"),
        "message": message,
    }


def commit_envelope(parsed: dict) -> dict:
    def field(raw: bytes, suffix: str) -> dict[str, str | None]:
        return {f"{suffix}_b64": b64encode_raw(raw), f"{suffix}_display": display(raw)}

    envelope: dict = {"oid": parsed["oid"], "parent_oid": parsed["parent_oid"]}
    envelope.update(field(parsed["author_name"], "author_name"))
    envelope.update(field(parsed["author_email"], "author_email"))
    envelope["authored_at"] = parsed["authored_at"]
    envelope["authored_timezone"] = parsed["authored_timezone"]
    envelope.update(field(parsed["committer_name"], "committer_name"))
    envelope.update(field(parsed["committer_email"], "committer_email"))
    envelope["committed_at"] = parsed["committed_at"]
    envelope["committed_timezone"] = parsed["committed_timezone"]
    envelope.update(field(parsed["message"], "message"))
    return envelope
