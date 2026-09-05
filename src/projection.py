"""Permission-view projection engine.

The retained simple reference projector (``project_tree``,
``project_history``, ``visible_advance``) flattens the complete canonical
tree per commit. The incremental projector below (``project_history_fast``,
``visible_advance_fast``, ``IncrementalProjector``) applies only changed
paths to the previous projected tree, compiles policy prefixes once per
descriptor, and batch-transfers each distinct visible blob OID at most once
per destination generation. Optimized outputs are byte-identical to the
reference projector.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from errors import ForgeError
from git import (
    author_environment,
    copy_object,
    first_parent_commits,
    git,
    make_commit,
    commit_message,
    tree_entries,
    tree_oid,
    validate_no_empty_subtrees,
    write_tree,
)
from layout import ZERO_OID
from validation import TreeEntries, parse_change_id
from validation import visible


def project_tree(
    canonical: Path,
    destination: Path,
    canonical_treeish: str,
    descriptor: dict,
    source_env: dict[str, str] | None = None,
    destination_env: dict[str, str] | None = None,
) -> str:
    visible_paths = {}
    validate_no_empty_subtrees(canonical, canonical_treeish, source_env)
    for path, entry in tree_entries(canonical, canonical_treeish, source_env).items():
        if not visible(path, descriptor):
            continue
        mode, kind, oid = entry
        if kind == b"blob":
            copy_object(canonical, destination, oid, source_env, destination_env)
        elif kind != b"commit":
            raise ForgeError("unsupported canonical tree entry")
        visible_paths[path] = (mode, kind, oid)
    return write_tree(destination, visible_paths, destination_env)


def synthetic_commit(
    canonical: Path,
    destination: Path,
    canonical_oid: str,
    projected_tree: str,
    projected_parent: str,
    change_id: str | None = None,
) -> str:
    metadata = author_environment(canonical, canonical_oid, projected=True)
    message = b"projected canonical change\n"
    if change_id:
        message += f"\nChange-Id: {change_id}\n".encode()
    return make_commit(
        destination,
        projected_tree,
        projected_parent,
        message,
        metadata,
    )


def visible_advance(
    canonical: Path,
    frontier: str,
    tip: str,
    destination: Path,
    descriptor: dict,
    expected_tree: str,
) -> None:
    previous_tree = expected_tree
    for oid in first_parent_commits(canonical, frontier, tip):
        projected = project_tree(canonical, destination, oid, descriptor)
        if projected != previous_tree:
            raise ForgeError(
                "visible history advanced; synchronize the view, fetch, and rebase"
            )
        previous_tree = projected


def project_history(
    canonical: Path,
    destination: Path,
    frontier: str,
    canonical_tip: str,
    projected_tip: str,
    descriptor: dict,
) -> tuple[str, str, dict[str, dict[str, str]]]:
    """Project canonical commits, eliding commits whose visible tree is unchanged."""
    previous_tree = (
        tree_oid(destination, projected_tip) if projected_tip != ZERO_OID else None
    )
    anchors: dict[str, dict[str, str]] = {}
    for canonical_oid in first_parent_commits(canonical, frontier, canonical_tip):
        projected_tree = project_tree(
            canonical, destination, canonical_oid, descriptor
        )
        if projected_tree != previous_tree:
            change_id = parse_change_id(commit_message(canonical, canonical_oid))
            projected_tip = synthetic_commit(
                canonical,
                destination,
                canonical_oid,
                projected_tree,
                projected_tip,
                change_id,
            )
            anchors[projected_tip] = {
                "canonical": canonical_oid,
                "kind": "synthetic",
                "change_id": change_id,
            }
            previous_tree = projected_tree
        frontier = canonical_oid
    return projected_tip, frontier, anchors


# ---------------------------------------------------------------------------
# Incremental projector.
# ---------------------------------------------------------------------------

_FAST_STATS = {
    "subprocesses": 0,
    "trees_validated": 0,
    "blobs_transferred": 0,
    "bytes_read": 0,
    "commits_advanced": 0,
}


def fast_projection_stats() -> dict[str, int]:
    return dict(_FAST_STATS)


def reset_fast_projection_stats() -> None:
    for key in _FAST_STATS:
        _FAST_STATS[key] = 0


@dataclass(frozen=True)
class ReadMatcher:
    """Immutable byte-oriented capabilities-v3 longest allow/deny matcher."""

    allow: tuple[bytes, ...]
    deny: tuple[bytes, ...]
    digest: str

    def visible(self, path: bytes) -> bool:
        grant_length = -1
        for prefix in self.allow:
            if not prefix or path == prefix or path.startswith(prefix + b"/"):
                if len(prefix) > grant_length:
                    grant_length = len(prefix)
        if grant_length < 0:
            return False
        restriction_length = -1
        for prefix in self.deny:
            if not prefix or path == prefix or path.startswith(prefix + b"/"):
                if len(prefix) > restriction_length:
                    restriction_length = len(prefix)
        if restriction_length < 0:
            return True
        return restriction_length < grant_length


_MATCHER_CACHE: dict[str, ReadMatcher] = {}


def compile_read_matcher(descriptor: dict) -> ReadMatcher:
    """Compile policy prefixes once per descriptor, preserving v3 semantics."""
    import hashlib as _hashlib
    import json as _json

    try:
        allow_raw = descriptor["capabilities"]["read"]
        deny_raw = descriptor["restrictions"]["read"]
        fingerprint = _hashlib.sha256(
            _json.dumps(
                {"allow": allow_raw, "deny": deny_raw}, sort_keys=True
            ).encode()
        ).hexdigest()
    except (KeyError, TypeError) as error:
        raise ForgeError("descriptor has no readable policy", "integrity") from error
    cached = _MATCHER_CACHE.get(descriptor.get("id", "") + "\x00" + fingerprint)
    if cached is not None:
        return cached
    allow = tuple(sorted(os.fsencode(prefix) for prefix in allow_raw))
    deny = tuple(sorted(os.fsencode(prefix) for prefix in deny_raw))
    matcher = ReadMatcher(allow=allow, deny=deny, digest=fingerprint)
    _MATCHER_CACHE[descriptor.get("id", "") + "\x00" + fingerprint] = matcher
    # Spot-check the compiled matcher against pointwise adjudication.
    probes = [b"", b"a", b"shared/file.txt"]
    for rule in list(allow_raw) + list(deny_raw):
        encoded = os.fsencode(rule)
        probes.extend((encoded, encoded + b"/leaf"))
    for probe in probes:
        if matcher.visible(probe) != visible(probe, descriptor):
            raise ForgeError("compiled policy matcher diverges", "integrity")
    return matcher


def _git_counted(repo: Path, *args, **kwargs):
    _FAST_STATS["subprocesses"] += 1
    return git(repo, *args, **kwargs)


def _changed_paths_fast(
    canonical: Path, old: str | None, new: str, env=None
) -> tuple[list[tuple[bytes, bytes]], list[bytes]]:
    """Return ([(status, path)...], [parent dirs...]) using one diff-tree.

    Scales with changed paths, not tree size. Rename/copies are reported as
    delete+add pairs (no rename detection), matching per-path semantics.
    """
    if old is None or old == ZERO_OID:
        raw = _git_counted(
            canonical,
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "-r",
            "-z",
            "--root",
            new,
            env=env,
        ).stdout
    else:
        raw = _git_counted(
            canonical,
            "diff-tree",
            "--no-commit-id",
            "--name-status",
            "-r",
            "-z",
            "--no-renames",
            old,
            new,
            env=env,
        ).stdout
    parts = raw.split(b"\0")
    changes: list[tuple[bytes, bytes]] = []
    index = 0
    while index < len(parts):
        status = parts[index]
        index += 1
        if not status:
            continue
        kind = status[:1]
        if kind in (b"A", b"D", b"M", b"T"):
            if index >= len(parts):
                break
            changes.append((kind, parts[index]))
            index += 1
        elif kind == b"R" or kind == b"C":
            index += 2
            continue
        else:
            # Unrecognized status: skip its path conservatively by treating
            # the next token as a path carrier.
            if index < len(parts):
                index += 1
    parents = sorted(
        {
            bytes(parent)
            for _kind, path in changes
            for parent in [os.path.dirname(path)]
            if parent
        }
    )
    return changes, parents


def _tree_level(repo: Path, treeish: str, env=None) -> dict[bytes, tuple[bytes, bytes, str]]:
    """Non-recursive level listing: name -> (mode, kind, oid)."""
    raw = _git_counted(repo, "ls-tree", "-z", treeish, env=env).stdout
    level: dict[bytes, tuple[bytes, bytes, str]] = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        mode, kind, oid = metadata.split(b" ")
        level[path] = (mode, kind, oid.decode("ascii"))
    return level


class IncrementalProjector:
    """Boundary-aware per-destination projector with batch object transfer.

    Changed paths are applied to the previous projected entries instead of
    flattening both snapshots per commit. Each distinct visible blob OID is
    read/transferred at most once per destination generation through one (or
    a bounded number of) batch ``cat-file``/``hash-object`` operations.
    """

    _FULL_LISTING_THRESHOLD = 1000

    def __init__(
        self,
        canonical: Path,
        destination: Path,
        descriptor: dict,
        source_env=None,
        destination_env=None,
    ):
        self.canonical = canonical
        self.destination = destination
        self.descriptor = descriptor
        self.matcher = compile_read_matcher(descriptor)
        self.source_env = source_env
        self.destination_env = destination_env
        self.stored: set[str] = set()

    def base_entries(self, projected_tip: str) -> TreeEntries:
        """Fetch the starting visible entries once per run (never per commit)."""
        if projected_tip == ZERO_OID:
            return {}
        tree = _git_counted(
            self.destination,
            "rev-parse",
            f"{projected_tip}^{{tree}}",
            env=self.destination_env,
        ).stdout.decode().strip()
        return self.base_entries_from_tree(tree)

    def base_entries_from_tree(self, tree: str) -> TreeEntries:
        """Fetch visible entries for an already-known projected tree OID."""
        return tree_entries(self.destination, tree, self.destination_env)

    def advance(
        self,
        previous: TreeEntries,
        old_canonical: str | None,
        new_canonical: str,
    ) -> TreeEntries:
        """Apply one canonical commit's changed paths to projected entries."""
        _FAST_STATS["commits_advanced"] += 1
        _validate_no_empty_subtrees_fast(
            self.canonical, old_canonical, new_canonical, self.source_env
        )
        changes, _parents = _changed_paths_fast(
            self.canonical, old_canonical, new_canonical, self.source_env
        )
        if len(changes) > self._FULL_LISTING_THRESHOLD:
            return self._advance_full(previous, new_canonical)
        # One scoped listing for every touched path, not a full-tree walk.
        touched: TreeEntries = {}
        if changes:
            paths = sorted({path for _kind, path in changes})
            touched = self._list_paths(new_canonical, paths)
        result = dict(previous)
        needed: dict[str, None] = {}
        for kind, path in changes:
            entry = touched.get(path)
            if entry is None:
                # Deleted path, or a path hidden behind a file/directory
                # replacement whose new form lists no leaf here.
                result.pop(path, None)
                continue
            mode, entry_kind, oid = entry
            if entry_kind == b"commit":
                # Gitlink passthrough mirrors the reference projector; the
                # object stays unresolved until closure validation decides.
                if self.matcher.visible(path):
                    result[path] = entry
                else:
                    result.pop(path, None)
                continue
            if entry_kind != b"blob":
                raise ForgeError("unsupported canonical tree entry")
            if self.matcher.visible(path):
                result[path] = entry
                needed[oid] = None
            else:
                result.pop(path, None)
        # Paths whose leaf changed only in visibility-relevant ancestry are
        # covered because diff-tree reports every blob whose OID changed, and
        # renames are reported as delete+add pairs.
        self._store_blobs(needed)
        from validation import has_file_directory_conflict

        if has_file_directory_conflict(result):
            raise ForgeError("tree transition has a file/directory conflict")
        return result

    def _list_paths(self, treeish: str, paths: list[bytes]) -> TreeEntries:
        """List exactly the touched paths in one subprocess.

        Pathspecs use ``:(literal)`` so unusual byte paths (glob characters,
        leading dashes or colons) cannot over- or under-match. Over-matching
        would be harmless (only changed paths are read), but literal matching
        keeps the subprocess bounded by the change.
        """
        args: list = ["ls-tree", "-z", treeish, "--"]
        for path in paths:
            args.append(":(literal)" + path.decode("utf-8", "surrogateescape"))
        try:
            raw = _git_counted(self.canonical, *args, env=self.source_env).stdout
        except ForgeError:
            # Any pathspec surprise fails over to one full listing rather
            # than a wrong verdict.
            return tree_entries(self.canonical, treeish, self.source_env)
        found: TreeEntries = {}
        for item in raw.split(b"\0"):
            if not item:
                continue
            metadata, path = item.split(b"\t", 1)
            mode, kind, oid = metadata.split(b" ")
            found[path] = (mode, kind, oid.decode("ascii"))
        return found

    def _advance_full(self, previous: TreeEntries, new_canonical: str) -> TreeEntries:
        """Mass-change fallback: one full listing instead of huge pathspecs."""
        result = dict(previous)
        current = tree_entries(self.canonical, new_canonical, self.source_env)
        needed: dict[str, None] = {}
        touched = set(current) | set(previous)
        for path in touched:
            entry = current.get(path)
            if entry is None:
                result.pop(path, None)
                continue
            mode, kind, oid = entry
            if kind != b"blob":
                if kind == b"commit":
                    if self.matcher.visible(path):
                        result[path] = entry
                    else:
                        result.pop(path, None)
                    continue
                raise ForgeError("unsupported canonical tree entry")
            if self.matcher.visible(path):
                result[path] = entry
                needed[oid] = None
            else:
                result.pop(path, None)
        self._store_blobs(needed)
        from validation import has_file_directory_conflict

        if has_file_directory_conflict(result):
            raise ForgeError("tree transition has a file/directory conflict")
        return result

    def _store_blobs(self, needed: dict[str, None]) -> None:
        """Ensure every needed blob OID exists in the destination, once each.

        Blobs transfer in bounded batches (at most ``_BLOB_BATCH`` OIDs per
        ``cat-file --batch`` round trip) so a large initial projection never
        holds multiple copies of the entire visible repository in memory.
        Each batch is parsed, staged, and hashed before the next begins.
        """
        fresh = [oid for oid in needed if oid not in self.stored]
        if not fresh:
            return
        query = ("".join(f"{oid}\n" for oid in fresh)).encode("ascii")
        listing = _git_counted(
            self.destination,
            "cat-file",
            "--batch-check=%(objectname)",
            data=query,
            env=self.destination_env,
        ).stdout.splitlines()
        missing = [oid for oid in fresh if oid.encode("ascii") not in listing]
        self.stored.update(oid for oid in fresh if oid not in missing)
        if not missing:
            return
        batch_size = 50
        with tempfile.TemporaryDirectory(prefix="bastion-projection-") as staging:
            for start_index in range(0, len(missing), batch_size):
                batch = missing[start_index : start_index + batch_size]
                payload = ("".join(f"{oid}\n" for oid in batch)).encode("ascii")
                raw = _git_counted(
                    self.canonical, "cat-file", "--batch", data=payload, env=self.source_env
                ).stdout
                _FAST_STATS["bytes_read"] += len(raw)
                names: list[str] = []
                cursor = 0
                for oid in batch:
                    header_end = raw.find(b"\n", cursor)
                    if header_end < 0:
                        raise ForgeError("object format mismatch while copying object")
                    fields = raw[cursor:header_end].split(b" ")
                    if len(fields) != 3 or fields[0] != oid.encode("ascii"):
                        if fields == [oid.encode("ascii"), b"missing"]:
                            raise ForgeError("canonical object is missing from its database")
                        raise ForgeError("object format mismatch while copying object")
                    if fields[1] != b"blob":
                        raise ForgeError("object format mismatch while copying object")
                    try:
                        size = int(fields[2])
                    except ValueError as error:
                        raise ForgeError("object format mismatch while copying object") from error
                    start = header_end + 1
                    blob = raw[start : start + size]
                    if len(blob) != size or raw[start + size : start + size + 1] != b"\n":
                        raise ForgeError("object format mismatch while copying object")
                    # Write immediately; only one batch of blobs is ever
                    # resident, and `blob` is released on the next iteration.
                    target = os.path.join(staging, oid)
                    with open(target, "wb") as handle:
                        handle.write(blob)
                    names.append(target)
                    cursor = start + size + 1
                    del blob
                produced = _git_counted(
                    self.destination,
                    "hash-object",
                    "-w",
                    "-t",
                    "blob",
                    "--stdin-paths",
                    data=("".join(f"{name}\n" for name in names)).encode(),
                    env=self.destination_env or {},
                ).stdout.decode().split()
                if produced != batch:
                    raise ForgeError("object format mismatch while copying object")
                _FAST_STATS["blobs_transferred"] += len(batch)
                self.stored.update(batch)
                del raw

    def write_projected_tree(self, entries: TreeEntries) -> str:
        return write_tree(self.destination, entries, self.destination_env)

    def advance_commit(
        self,
        previous_entries: TreeEntries,
        old_canonical: str | None,
        new_canonical: str,
    ) -> tuple[TreeEntries, str]:
        """Apply one commit and return (new entries, new projected tree OID)."""
        entries = self.advance(previous_entries, old_canonical, new_canonical)
        return entries, self.write_projected_tree(entries)


def project_history_fast(
    canonical: Path,
    destination: Path,
    frontier: str,
    canonical_tip: str,
    projected_tip: str,
    descriptor: dict,
    source_env=None,
    destination_env=None,
) -> tuple[str, str, dict[str, dict[str, str]]]:
    """Incremental history projection with byte-identical reference output.

    Changed paths are applied to the previous projected tree instead of
    flattening both snapshots for each commit.
    """
    projector = IncrementalProjector(
        canonical, destination, descriptor, source_env, destination_env
    )
    entries = projector.base_entries(projected_tip)
    previous_tree = (
        tree_oid(destination, projected_tip, destination_env)
        if projected_tip != ZERO_OID
        else None
    )
    anchors: dict[str, dict[str, str]] = {}
    old_canonical: str | None = None if projected_tip == ZERO_OID else frontier
    for canonical_oid in first_parent_commits(canonical, frontier, canonical_tip):
        entries, projected_tree = projector.advance_commit(
            entries, old_canonical, canonical_oid
        )
        if projected_tree != previous_tree:
            change_id = parse_change_id(
                commit_message(canonical, canonical_oid, source_env)
            )
            projected_tip = make_commit(
                destination,
                projected_tree,
                projected_tip,
                _synthetic_message(change_id),
                author_environment(canonical, canonical_oid, projected=True),
                destination_env,
            )
            anchors[projected_tip] = {
                "canonical": canonical_oid,
                "kind": "synthetic",
                "change_id": change_id,
            }
            previous_tree = projected_tree
        frontier = canonical_oid
        old_canonical = canonical_oid
    return projected_tip, frontier, anchors


def _synthetic_message(change_id: str | None) -> bytes:
    message = b"projected canonical change\n"
    if change_id:
        message += f"\nChange-Id: {change_id}\n".encode()
    return message


def visible_advance_fast(
    canonical: Path,
    frontier: str,
    tip: str,
    destination: Path,
    descriptor: dict,
    expected_tree: str,
    source_env=None,
    destination_env=None,
) -> None:
    """Incremental visible-advance check with reference-identical verdicts.

    The tip is projected completely once and must equal the expected tree;
    earlier commits are then verified by reverting each commit's changed
    paths (bounded by the change, transferring no blobs) and requiring every
    intermediate projected tree to equal the expected tree as well.
    """
    commits = first_parent_commits(canonical, frontier, tip)
    if not commits:
        return
    projector = IncrementalProjector(
        canonical, destination, descriptor, source_env, destination_env
    )
    tip_projected = project_tree(
        canonical, destination, tip, descriptor, source_env, destination_env
    )
    if tip_projected != expected_tree:
        raise ForgeError(
            "visible history advanced; synchronize the view, fetch, and rebase"
        )
    entries = tree_entries(destination, tip_projected, destination_env)
    # Every commit's projection is verified exactly once; the frontier itself
    # is never examined, matching the reference loop over (frontier..tip].
    # The oldest commit's changed levels are validated against the frontier
    # as well (new-side only, so no verdict can differ from the reference:
    # a flagged level is genuinely empty in that commit).
    if len(commits) > 1:
        _validate_no_empty_subtrees_fast(
            canonical, frontier, commits[0], projector.source_env
        )
    newer = tip
    for position in range(len(commits) - 1, 0, -1):
        older = commits[position - 1]
        changes, _parents = _changed_paths_fast(
            canonical, older, newer, projector.source_env
        )
        _validate_no_empty_subtrees_fast(
            canonical, older, newer, projector.source_env
        )
        old_listed = projector._list_paths(older, sorted({path for _k, path in changes})) if changes else {}
        for _kind, path in changes:
            old_entry = old_listed.get(path)
            if old_entry is None:
                entries.pop(path, None)
                continue
            mode, kind, entry_oid = old_entry
            if kind == b"commit":
                if projector.matcher.visible(path):
                    entries[path] = old_entry
                else:
                    entries.pop(path, None)
                continue
            if kind != b"blob":
                raise ForgeError("unsupported canonical tree entry")
            if projector.matcher.visible(path):
                entries[path] = old_entry
            else:
                entries.pop(path, None)
        reverted = projector.write_projected_tree(entries)
        if reverted != expected_tree:
            raise ForgeError(
                "visible history advanced; synchronize the view, fetch, and rebase"
            )
        newer = older


def _validate_no_empty_subtrees_fast(
    canonical: Path, old: str | None, new: str, env=None
) -> None:
    """Exact empty-subtree check bounded by changed levels, not tree size.

    Any structural addition changes tree OIDs up its ancestor chain, so
    walking only levels whose tree OID differs (or which are new) finds every
    newly introduced empty subtree without enumerating hidden subtrees.
    """
    _FAST_STATS["trees_validated"] += 1
    if old is None or old == ZERO_OID:
        validate_no_empty_subtrees(canonical, new, env)
        return
    old_root = _git_counted(
        canonical, "rev-parse", f"{old}^{{tree}}", env=env
    ).stdout.decode().strip()
    new_root = _git_counted(
        canonical, "rev-parse", f"{new}^{{tree}}", env=env
    ).stdout.decode().strip()
    if old_root == new_root:
        return
    queue: list[tuple[str | None, str]] = [(old_root, new_root)]
    while queue:
        old_oid, new_oid = queue.pop()
        level = _tree_level(canonical, new_oid, env)
        if not level:
            raise ForgeError("explicit empty subtrees are not supported")
        old_level = _tree_level(canonical, old_oid, env) if old_oid else {}
        for name, (_mode, kind, oid) in level.items():
            if kind != b"tree":
                continue
            counterpart = old_level.get(name)
            if counterpart is None or counterpart[2] != oid:
                queue.append(
                    (
                        counterpart[2]
                        if counterpart is not None and counterpart[1] == b"tree"
                        else None,
                        oid,
                    )
                )
