"""Read-only global invariant checks for a version-1 Bastion forge."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
from pathlib import Path
from typing import Any

from errors import ForgeError
from git import clean_env, git, ref, tree_entries
from layout import ZERO_OID, canonical_repo, view_repo
from outbox import event_id, job_id
from protocol import transaction_id
from state import is_ancestor, load_state
from validation import (
    ALLOWED_MODES,
    allowed,
    apply_changes,
    authorize_changes,
    changed_entries,
    normalize_prefix,
    parse_change_id,
    validate_view_name,
)


OID = re.compile(r"^[0-9a-f]{40}$")
SOURCE_STATUSES = {"prepared", "canonical_committed", "source_unconfirmed", "fanout_pending", "published", "aborted"}
ADMIN_STATUSES = {"prepared", "fanout_pending", "published", "aborted"}
EVENT_STATUSES = {"blocked", "pending", "dispatched"}
JOB_STATUSES = {"pending", "running", "ready", "retry", "completed"}


def _fail(code: str, subject: object, detail: str) -> None:
    raise ForgeError(f"invariant {code} failed for {subject}: {detail}")


def _path(path: bytes) -> str:
    return repr(os.fsdecode(path))


def _is_int(value: object, minimum: int | None = None) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and (minimum is None or value >= minimum)


def _oid(value: object) -> bool:
    return isinstance(value, str) and value != ZERO_OID and bool(OID.fullmatch(value))


def _required(record: dict, fields: set[str], subject: str) -> None:
    missing = sorted(fields - record.keys())
    if missing:
        _fail("STATE_SCHEMA", subject, f"missing required field {missing[0]}")


def _string(record: dict, field: str, subject: str, nullable: bool = False) -> None:
    value = record.get(field)
    if (nullable and value is None) or isinstance(value, str):
        return
    _fail("STATE_SCHEMA", subject, f"{field} has the wrong type")


def _record_maps(state: dict) -> None:
    required = {
        "version", "canonical_branch", "views", "transactions", "changes", "outbox", "projection_jobs", "next_transaction_sequence"
    }
    if set(state) != required:
        _fail("STATE_SCHEMA", "state", "top-level keys do not match version 1")
    if state["version"] != 1 or isinstance(state["version"], bool):
        _fail("STATE_SCHEMA", "state", "version must be integer 1")
    if not isinstance(state["canonical_branch"], str) or not state["canonical_branch"]:
        _fail("STATE_SCHEMA", "state", "canonical_branch must be a non-empty string")
    if not _is_int(state["next_transaction_sequence"], 1):
        _fail("STATE_SCHEMA", "state", "next_transaction_sequence must be a positive integer")
    for name in ("views", "transactions", "changes", "outbox", "projection_jobs"):
        if not isinstance(state[name], dict):
            _fail("STATE_SCHEMA", "state", f"{name} must be a dictionary")
        for key, value in state[name].items():
            if not isinstance(key, str) or not isinstance(value, dict):
                _fail("STATE_SCHEMA", name, "record identifiers and records must be strings and dictionaries")


def _schema(state: dict) -> None:
    _record_maps(state)
    if git(Path("."), "check-ref-format", "--branch", state["canonical_branch"], check=False).returncode:
        _fail("STATE_SCHEMA", "state", "canonical_branch is not a valid branch")
    for name, view in sorted(state["views"].items()):
        try:
            validate_view_name(name)
        except ForgeError:
            _fail("STATE_SCHEMA", name, "invalid view name")
        _required(view, {"principal", "canonical_ref", "projected_ref", "read", "write", "canonical_frontier", "projected_tip", "anchors"}, name)
        for field in ("principal", "canonical_ref", "projected_ref", "canonical_frontier", "projected_tip"):
            _string(view, field, name)
        for field in ("read", "write"):
            if not isinstance(view[field], list) or not all(isinstance(item, str) for item in view[field]):
                _fail("STATE_SCHEMA", name, f"{field} must be a list of strings")
        if not isinstance(view["anchors"], dict):
            _fail("STATE_SCHEMA", name, "anchors must be a dictionary")
        for projected, anchor in view["anchors"].items():
            if not isinstance(projected, str) or not isinstance(anchor, dict):
                _fail("STATE_SCHEMA", name, "anchors must map strings to dictionaries")
            if not _oid(projected):
                _fail("STATE_SCHEMA", name, "anchor key is not a full object ID")
            _required(anchor, {"canonical", "kind"}, projected)
            if not _oid(anchor["canonical"]) or anchor["kind"] not in {"submitted", "synthetic"}:
                _fail("STATE_SCHEMA", projected, "invalid anchor")
            if anchor["kind"] == "submitted" and set(anchor) != {"canonical", "kind"}:
                _fail("STATE_SCHEMA", projected, "submitted anchor has unexpected fields")
            if anchor["kind"] == "synthetic":
                _required(anchor, {"canonical", "kind", "change_id"}, projected)
                if anchor["change_id"] is not None and not isinstance(anchor["change_id"], str):
                    _fail("STATE_SCHEMA", projected, "synthetic change_id must be string or null")
    sequences: set[int] = set()
    max_sequence = 0
    for identifier, transaction in sorted(state["transactions"].items()):
        _required(transaction, {"id", "sequence", "canonical_ref", "canonical_old", "canonical_new", "pending_views", "status"}, identifier)
        if transaction["id"] != identifier:
            _fail("STATE_SCHEMA", identifier, "transaction key does not match id")
        if not _is_int(transaction["sequence"], 1) or transaction["sequence"] in sequences:
            _fail("STATE_SCHEMA", identifier, "transaction sequence is invalid or duplicated")
        sequences.add(transaction["sequence"]); max_sequence = max(max_sequence, transaction["sequence"])
        for field in ("id", "canonical_ref", "canonical_old", "canonical_new", "status"):
            _string(transaction, field, identifier)
        if not _oid(transaction["canonical_old"]) or not _oid(transaction["canonical_new"]):
            _fail("STATE_SCHEMA", identifier, "canonical IDs must be nonzero full object IDs")
        if not isinstance(transaction["pending_views"], list) or not all(isinstance(item, str) for item in transaction["pending_views"]):
            _fail("STATE_SCHEMA", identifier, "pending_views must be a list of strings")
        if transaction.get("kind") == "canonical_update":
            if transaction["status"] not in ADMIN_STATUSES:
                _fail("STATE_SCHEMA", identifier, "invalid administrative status")
        elif "kind" in transaction:
            _fail("STATE_SCHEMA", identifier, "unknown transaction kind")
        else:
            _required(transaction, {"view", "principal", "projected_ref", "canonical_frontier_old", "projected_old", "projected_new", "mappings"}, identifier)
            if transaction["status"] not in SOURCE_STATUSES:
                _fail("STATE_SCHEMA", identifier, "invalid source status")
            for field in ("view", "principal", "projected_ref", "canonical_frontier_old", "projected_old", "projected_new"):
                _string(transaction, field, identifier)
                if field.endswith("old") or field.endswith("new"):
                    if not _oid(transaction[field]):
                        _fail("STATE_SCHEMA", identifier, f"{field} must be a nonzero full object ID")
            if not isinstance(transaction["mappings"], list):
                _fail("STATE_SCHEMA", identifier, "mappings must be a list")
            for mapping in transaction["mappings"]:
                if not isinstance(mapping, dict) or set(mapping) != {"submitted", "canonical", "change_id"}:
                    _fail("STATE_SCHEMA", identifier, "mapping has invalid shape")
                if not _oid(mapping["submitted"]) or not _oid(mapping["canonical"]) or (mapping["change_id"] is not None and not isinstance(mapping["change_id"], str)):
                    _fail("STATE_SCHEMA", identifier, "mapping has invalid values")
        if "event_id" in transaction and not isinstance(transaction["event_id"], str):
            _fail("STATE_SCHEMA", identifier, "event_id must be a string")
        if "view_errors" in transaction and (
            not isinstance(transaction["view_errors"], dict)
            or not all(isinstance(key, str) and isinstance(value, str) for key, value in transaction["view_errors"].items())
        ):
            _fail("STATE_SCHEMA", identifier, "view_errors must map strings to strings")
    if state["next_transaction_sequence"] <= max_sequence:
        _fail("STATE_SCHEMA", "state", "next_transaction_sequence does not exceed transactions")
    for identifier, event in sorted(state["outbox"].items()):
        _required(event, {"id", "type", "transaction_id", "sequence", "canonical_to", "target_views", "status"}, identifier)
        if event["id"] != identifier or not isinstance(event["id"], str): _fail("STATE_SCHEMA", identifier, "event key does not match id")
        if not isinstance(event["transaction_id"], str) or not _is_int(event["sequence"], 1) or not _oid(event["canonical_to"]) or not isinstance(event["target_views"], list) or not all(isinstance(value, str) for value in event["target_views"]) or event["status"] not in EVENT_STATUSES or not isinstance(event["type"], str):
            _fail("STATE_SCHEMA", identifier, "event has invalid field types")
    for identifier, job in sorted(state["projection_jobs"].items()):
        _required(job, {"id", "event_id", "transaction_id", "view", "sequence", "canonical_to", "status", "attempts", "last_error", "next_attempt_at"}, identifier)
        if job["id"] != identifier or not isinstance(job["id"], str): _fail("STATE_SCHEMA", identifier, "job key does not match id")
        if not all(isinstance(job[field], str) for field in ("event_id", "transaction_id", "view", "status")) or not _is_int(job["sequence"], 1) or not _oid(job["canonical_to"]) or job["status"] not in JOB_STATUSES or not _is_int(job["attempts"], 0) or (job["last_error"] is not None and not isinstance(job["last_error"], str)) or not isinstance(job["next_attempt_at"], (int, float)):
            _fail("STATE_SCHEMA", identifier, "job has invalid field types")
        for field in ("expected_frontier", "expected_projected_tip"):
            if field in job and not _oid(job[field]): _fail("STATE_SCHEMA", identifier, f"{field} is invalid")
        for field in ("lease_token",):
            if field in job and job[field] is not None and not isinstance(job[field], str):
                _fail("STATE_SCHEMA", identifier, f"{field} has the wrong type")
        if "lease_expires_at" in job and not isinstance(job["lease_expires_at"], (int, float)):
            _fail("STATE_SCHEMA", identifier, "lease_expires_at must be numeric")
        if "plan" in job and not isinstance(job["plan"], dict): _fail("STATE_SCHEMA", identifier, "plan must be a dictionary")
    for change_id, change in sorted(state["changes"].items()):
        if not isinstance(change_id, str) or not isinstance(change.get("revisions"), list):
            _fail("STATE_SCHEMA", str(change_id), "change has invalid shape")


def _refs(repo: Path, code: str, subject: str) -> dict[str, tuple[str, str]]:
    result = git(repo, "for-each-ref", "--format=%(refname) %(objectname) %(objecttype)", check=False)
    if result.returncode:
        _fail(code, subject, "cannot enumerate refs")
    refs: dict[str, tuple[str, str]] = {}
    for raw in result.stdout.splitlines():
        fields = raw.decode(errors="replace").split()
        if len(fields) != 3 or not _oid(fields[1]): _fail(code, subject, "malformed ref inventory")
        refs[fields[0]] = (fields[1], fields[2])
    return refs


def _bare_sha1(repo: Path, subject: str) -> None:
    if not repo.is_dir() or git(repo, "rev-parse", "--is-bare-repository", check=False).stdout.strip() != b"true" or git(repo, "rev-parse", "--show-object-format", check=False).stdout.strip() != b"sha1":
        _fail("FORGE_LAYOUT", subject, "not a readable bare SHA-1 repository")


def _layout_and_refs(root: Path, state: dict) -> tuple[dict[str, tuple[str, str]], dict[str, dict[str, tuple[str, str]]]]:
    canonical = canonical_repo(root)
    _bare_sha1(canonical, "canonical.git")
    views_dir = root / "views"
    if not views_dir.is_dir(): _fail("FORGE_LAYOUT", "views", "missing views directory")
    canonical_refs = _refs(canonical, "CANONICAL_REF", "canonical.git")
    view_refs: dict[str, dict[str, tuple[str, str]]] = {}
    on_disk = {path.name[:-4] for path in views_dir.glob("*.git") if path.is_dir()}
    if on_disk != set(state["views"]): _fail("FORGE_LAYOUT", "views", "state views and repositories differ")
    for name in sorted(state["views"]):
        repo = view_repo(root, name); _bare_sha1(repo, name)
        if (repo / "objects" / "info" / "alternates").exists() or (repo / "shallow").exists() or (repo / "info" / "grafts").exists():
            _fail("FORGE_LAYOUT", name, "unsafe repository storage mechanism")
        inventory = _refs(repo, "VIEW_REF", name)
        if any(key.startswith("refs/replace/") for key in inventory): _fail("FORGE_LAYOUT", name, "replace refs are forbidden")
        view_refs[name] = inventory
    return canonical_refs, view_refs


def _check_refs(root: Path, state: dict, canonical_refs: dict, view_refs: dict) -> str:
    canonical_name = f"refs/heads/{state['canonical_branch']}"
    target = canonical_refs.get(canonical_name)
    if not target or target[1] != "commit": _fail("CANONICAL_REF", canonical_name, "configured ref is not a commit")
    for name, (oid, kind) in sorted(canonical_refs.items()):
        if name.startswith("refs/replace/"): _fail("CANONICAL_REF", name, "replace refs are forbidden")
        if kind not in {"commit", "tree", "blob", "tag"} or git(canonical_repo(root), "cat-file", "-e", f"{oid}^{{{kind}}}", check=False).returncode:
            _fail("CANONICAL_REF", name, "ref target cannot be read")
    for name, view in sorted(state["views"].items()):
        expected = canonical_name
        if view["canonical_ref"] != expected or view["projected_ref"] != expected:
            _fail("VIEW_REF", name, "view refs do not match configured canonical branch")
        symbolic = git(view_repo(root, name), "symbolic-ref", "-q", "HEAD", check=False)
        if symbolic.returncode or symbolic.stdout.decode().strip() != expected: _fail("VIEW_REF", name, "HEAD does not target projected ref")
        inventory = view_refs[name]
        if set(inventory) != {expected} or inventory[expected][1] != "commit": _fail("VIEW_REF", name, "view has unexpected refs")
        if inventory[expected][0] != view["projected_tip"]: _fail("VIEW_REF", name, "published ref differs from persisted tip")
    return target[0]


def _raw_commit(repo: Path, oid: str, code: str, subject: str) -> bytes:
    result = git(repo, "cat-file", "commit", oid, check=False)
    if result.returncode: _fail(code, subject, "commit cannot be read")
    return result.stdout


def _commit_parts(raw: bytes, code: str, subject: str) -> tuple[str, list[str], list[bytes], bytes]:
    try:
        header, message = raw.split(b"\n\n", 1)
        lines = header.split(b"\n")
        tree = lines[0].split(b" ", 1)[1].decode("ascii") if lines[0].startswith(b"tree ") else ""
        parents = [line.split(b" ", 1)[1].decode("ascii") for line in lines if line.startswith(b"parent ")]
    except (IndexError, UnicodeDecodeError, ValueError):
        _fail(code, subject, "malformed raw commit")
    if not _oid(tree) or not all(_oid(parent) for parent in parents): _fail(code, subject, "malformed commit object IDs")
    return tree, parents, lines, message


def _history(repo: Path, tip: str, code: str, subject: str) -> list[str]:
    result = git(repo, "rev-list", "--reverse", "--first-parent", tip, check=False)
    if result.returncode: _fail(code, subject, "cannot walk history")
    values = result.stdout.decode().splitlines()
    previous: str | None = None
    for oid in values:
        raw = _raw_commit(repo, oid, code, subject); _tree, parents, _lines, _message = _commit_parts(raw, code, oid)
        if parents != ([] if previous is None else [previous]): _fail(code, oid, "history is not a linear first-parent chain")
        previous = oid
    return values


def _flat(repo: Path, oid: str, code: str) -> dict[bytes, tuple[bytes, bytes, str]]:
    try:
        entries = tree_entries(repo, oid)
    except ForgeError:
        _fail(code, oid, "tree cannot be read")
    for path, (mode, kind, object_id) in sorted(entries.items()):
        if mode not in ALLOWED_MODES or kind != b"blob": _fail(code, _path(path), "unsupported tree leaf")
        if not _oid(object_id) or git(repo, "cat-file", "-e", f"{object_id}^{{blob}}", check=False).returncode:
            _fail(code, _path(path), "missing blob")
    return entries


def _no_empty(repo: Path, oid: str, code: str) -> set[str]:
    result = git(repo, "ls-tree", "-rzt", "--full-tree", oid, check=False)
    if result.returncode: _fail(code, oid, "tree cannot be read")
    trees: list[bytes] = []; leaves: list[bytes] = []; tree_oids: set[str] = set()
    try:
        for item in result.stdout.split(b"\0"):
            if not item: continue
            metadata, path = item.split(b"\t", 1); _mode, kind, object_id = metadata.split(b" ")
            oid_str = object_id.decode("ascii")
            if not _oid(oid_str): _fail(code, _path(path), "invalid object ID")
            if kind == b"tree":
                trees.append(path); tree_oids.add(oid_str)
            else:
                leaves.append(path)
    except (ValueError, UnicodeDecodeError): _fail(code, oid, "malformed tree output")
    for tree in trees:
        if not any(path.startswith(tree + b"/") for path in leaves): _fail(code, _path(tree), "explicit empty subtree")
    return tree_oids


def _canonical_graph(root: Path, tip: str) -> list[str]:
    canonical = canonical_repo(root)
    if git(canonical, "fsck", "--full", "--strict", "--no-reflogs", "--no-dangling", check=False).returncode:
        _fail("CANONICAL_GRAPH", "canonical.git", "strict fsck failed")
    history = _history(canonical, tip, "CANONICAL_GRAPH", "canonical")
    for oid in history:
        tree, _parents, _lines, _message = _commit_parts(_raw_commit(canonical, oid, "CANONICAL_GRAPH", oid), "CANONICAL_GRAPH", oid)
        if git(canonical, "cat-file", "-e", f"{tree}^{{tree}}", check=False).returncode: _fail("CANONICAL_GRAPH", oid, "missing root tree")
        _no_empty(canonical, oid, "CANONICAL_GRAPH"); _flat(canonical, oid, "CANONICAL_GRAPH")
    return history


def _filtered(entries: dict, prefixes: list[str]) -> dict:
    return {path: entry for path, entry in entries.items() if allowed(path, prefixes)}


def _policy_frontiers(root: Path, state: dict, canonical_history: list[str], tip: str) -> None:
    index = {oid: position for position, oid in enumerate(canonical_history)}
    for name, view in sorted(state["views"].items()):
        if view["principal"] != name: _fail("FRONTIER", name, "principal differs from view name")
        for field in ("read", "write"):
            values = view[field]
            try: normalized = [normalize_prefix(value) for value in values]
            except ForgeError: _fail("FRONTIER", name, f"invalid {field} prefix")
            if values != normalized or values != sorted(set(values)): _fail("FRONTIER", name, f"{field} prefixes are not normalized, sorted, and unique")
        if not view["read"]: _fail("FRONTIER", name, "read policy is empty")
        if view["canonical_frontier"] not in index: _fail("FRONTIER", name, "frontier is not on canonical history")
        if view["canonical_frontier"] != tip:
            explain = [tx for tx in state["transactions"].values() if tx["status"] != "aborted" and tx.get("canonical_new") == tip and ((name in tx.get("pending_views", [])) or (tx.get("kind") != "canonical_update" and tx["status"] in {"prepared", "canonical_committed", "source_unconfirmed"}))]
            if not explain or not any(index.get(tx["canonical_new"], -1) > index[view["canonical_frontier"]] for tx in explain): _fail("FRONTIER", name, "frontier lag has no durable explanation")


def _identity(lines: list[bytes], prefix: bytes, code: str, subject: str) -> tuple[bytes, bytes, bytes]:
    matches = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    if len(matches) != 1: _fail(code, subject, f"missing or duplicate {prefix.decode()} header")
    match = re.fullmatch(rb"(.*) <([^<>]*)> ([0-9]+ [+-][0-9]{4})", matches[0])
    if not match: _fail(code, subject, "malformed identity header")
    return match.group(1), match.group(2), match.group(3)


def _source_transactions(root: Path, state: dict, canonical_history: list[str], tip: str) -> None:
    canonical = canonical_repo(root); index = {oid: number for number, oid in enumerate(canonical_history)}
    for identifier, tx in sorted(state["transactions"].items(), key=lambda item: (item[1]["sequence"], item[0])):
        if tx.get("kind") == "canonical_update": continue
        view = state["views"].get(tx["view"])
        if not view or tx["principal"] != tx["view"] or tx["principal"] != view["principal"]: _fail("MAPPING", identifier, "source view or principal is invalid")
        if tx["canonical_ref"] != view["canonical_ref"] or tx["projected_ref"] != view["projected_ref"] or tx["id"] != transaction_id(tx["view"], tx["projected_old"], tx["projected_new"]): _fail("MAPPING", identifier, "source transaction identity or refs differ")
        if not tx["mappings"]: _fail("MAPPING", identifier, "source transaction has no mappings")
        if len(tx["pending_views"]) != len(set(tx["pending_views"])) or tx["view"] in tx["pending_views"] or any(value not in state["views"] for value in tx["pending_views"]): _fail("MAPPING", identifier, "invalid pending views")
        if tx["canonical_frontier_old"] not in index or tx["canonical_old"] not in index or index[tx["canonical_frontier_old"]] > index[tx["canonical_old"]]: _fail("MAPPING", identifier, "invalid canonical frontier range")
        for oid in canonical_history[index[tx["canonical_frontier_old"]]:index[tx["canonical_old"]] + 1]:
            if _filtered(_flat(canonical, oid, "MAPPING"), view["read"]) != _flat(view_repo(root, tx["view"]), tx["projected_old"], "MAPPING"): _fail("MAPPING", identifier, "canonical prehistory was not hidden-only")
        submitted_parent = tx["projected_old"]; canonical_parent = tx["canonical_old"]; submitted_repo = view_repo(root, tx["view"])
        for mapping in tx["mappings"]:
            submitted = mapping["submitted"]; canon = mapping["canonical"]
            _tree, parents, submitted_lines, message = _commit_parts(_raw_commit(submitted_repo, submitted, "MAPPING", identifier), "MAPPING", submitted)
            _ctree, cparents, canonical_lines, canonical_message = _commit_parts(_raw_commit(canonical, canon, "MAPPING", identifier), "MAPPING", canon)
            if parents != [submitted_parent] or cparents != [canonical_parent]: _fail("MAPPING", identifier, "submitted or canonical mapping chain is discontinuous")
            if any(
                not line.startswith((b"tree ", b"parent ", b"author ", b"committer "))
                for line in canonical_lines
            ):
                _fail("MAPPING", identifier, "canonical mapping commit has unsupported metadata")
            submitted_parent_entries = _flat(submitted_repo, submitted_parent, "MAPPING"); submitted_entries = _flat(submitted_repo, submitted, "MAPPING")
            changes = changed_entries(submitted_parent_entries, submitted_entries)
            try: authorize_changes(changes, view["read"], view["write"]); applied = apply_changes(_flat(canonical, canonical_parent, "MAPPING"), changes)
            except ForgeError: _fail("MAPPING", identifier, "mapping contains unauthorized or invalid changes")
            if applied != _flat(canonical, canon, "MAPPING") or _filtered(_flat(canonical, canon, "MAPPING"), view["read"]) != submitted_entries: _fail("MAPPING", identifier, "mapping tree does not reconcile exactly")
            if message != canonical_message or mapping["change_id"] != parse_change_id(message): _fail("MAPPING", identifier, "mapping message or Change-Id differs")
            if _identity(submitted_lines, b"author ", "MAPPING", submitted) != _identity(canonical_lines, b"author ", "MAPPING", canon): _fail("MAPPING", identifier, "canonical author differs from submission")
            _name, _email, author_date = _identity(submitted_lines, b"author ", "MAPPING", submitted)
            committer = _identity(canonical_lines, b"committer ", "MAPPING", canon)
            if committer != (b"Bastion Forge", b"forge@localhost", author_date): _fail("MAPPING", identifier, "canonical committer metadata differs")
            submitted_parent = submitted; canonical_parent = canon
        if submitted_parent != tx["projected_new"] or canonical_parent != tx["canonical_new"]: _fail("MAPPING", identifier, "mapping endpoints differ")
        actual = ref(submitted_repo, tx["projected_ref"])
        if tx["status"] in {"fanout_pending", "published"} and (not is_ancestor(submitted_repo, tx["projected_new"], actual) or any(mapping["submitted"] not in view["anchors"] for mapping in tx["mappings"])): _fail("MAPPING", identifier, "confirmed source publication is missing")
        if tx["status"] in {"canonical_committed", "source_unconfirmed"} and (actual != tx["projected_old"] or view["canonical_frontier"] != tx["canonical_frontier_old"]): _fail("MAPPING", identifier, "unconfirmed source cursor moved")
        if tx["status"] in {"canonical_committed", "source_unconfirmed", "fanout_pending", "published"} and tx["canonical_new"] not in index: _fail("CANONICAL_REF", identifier, "committed canonical endpoint is not published")
        if tx["status"] == "prepared" and tip not in {tx["canonical_old"], tx["canonical_new"]}: _fail("CANONICAL_REF", identifier, "prepared transaction has unrelated canonical tip")


def _admin_transactions(root: Path, state: dict, canonical_history: list[str], tip: str) -> None:
    canonical = canonical_repo(root); index = {oid: i for i, oid in enumerate(canonical_history)}; selected: list[dict] = []
    for identifier, tx in sorted(state["transactions"].items(), key=lambda item: (item[1]["sequence"], item[0])):
        if tx.get("kind") == "canonical_update":
            if tx["id"] != f"canonical:{tx['sequence']}:{tx['canonical_new']}" or tx["canonical_ref"] != f"refs/heads/{state['canonical_branch']}" or git(canonical, "cat-file", "-e", f"{tx['canonical_old']}^{{commit}}", check=False).returncode or git(canonical, "cat-file", "-e", f"{tx['canonical_new']}^{{commit}}", check=False).returncode or not is_ancestor(canonical, tx["canonical_old"], tx["canonical_new"]): _fail("CANONICAL_REF", identifier, "invalid administrative transaction")
            result = git(canonical, "rev-list", "--reverse", "--first-parent", f"{tx['canonical_old']}..{tx['canonical_new']}", check=False)
            if result.returncode: _fail("CANONICAL_REF", identifier, "cannot walk administrative history")
            previous = tx["canonical_old"]
            for oid in result.stdout.decode().splitlines():
                tree, parents, _lines, _message = _commit_parts(_raw_commit(canonical, oid, "CANONICAL_REF", identifier), "CANONICAL_REF", oid)
                if parents != [previous]: _fail("CANONICAL_REF", identifier, "administrative history is not linear")
                if git(canonical, "cat-file", "-e", f"{tree}^{{tree}}", check=False).returncode: _fail("CANONICAL_REF", oid, "missing root tree")
                _no_empty(canonical, oid, "CANONICAL_REF"); _flat(canonical, oid, "CANONICAL_REF")
                previous = oid
            if previous != tx["canonical_new"]: _fail("CANONICAL_REF", identifier, "administrative history does not reach endpoint")
            if len(tx["pending_views"]) != len(set(tx["pending_views"])) or any(view not in state["views"] for view in tx["pending_views"]): _fail("MAPPING", identifier, "invalid administrative pending views")
            if tx["status"] in {"fanout_pending", "published"} and tx["canonical_new"] not in index: _fail("CANONICAL_REF", identifier, "administrative endpoint is not published")
        active = (tx.get("kind") == "canonical_update" and tx["status"] in {"fanout_pending", "published"}) or (tx.get("kind") != "canonical_update" and tx["status"] in {"canonical_committed", "source_unconfirmed", "fanout_pending", "published"}) or (tx["status"] == "prepared" and tx["canonical_new"] == tip)
        if active: selected.append(tx)
    selected.sort(key=lambda tx: (tx["sequence"], tx["id"]))
    for previous, current in zip(selected, selected[1:]):
        if current["canonical_old"] != previous["canonical_new"]: _fail("CANONICAL_REF", current["id"], "canonical transaction chain is not adjacent")
    if selected and selected[-1]["canonical_new"] != tip: _fail("CANONICAL_REF", selected[-1]["id"], "transaction chain does not reach canonical tip")


def _synthetic(raw: bytes, projected: str, canonical: str, parent: str | None, change_id: str | None, code: str) -> None:
    tree, parents, _lines, _message = _commit_parts(raw, code, projected)
    expected = f"tree {tree}\n".encode() + (f"parent {parent}\n".encode() if parent else b"") + b"author Bastion Projection <projection@localhost> 946684800 +0000\ncommitter Bastion Projection <projection@localhost> 946684800 +0000\n\nprojected canonical change\n" + (f"\nChange-Id: {change_id}\n".encode() if change_id else b"")
    if raw != expected or parents != ([] if parent is None else [parent]): _fail(code, projected, "synthetic commit bytes or parent are not deterministic")


def _projected(root: Path, state: dict, canonical_history: list[str]) -> tuple[dict[str, list[tuple[str, str, str | None]]], dict[str, tuple[set[str], set[str], set[tuple[bytes, str]]]]]:
    canonical = canonical_repo(root); index = {oid: i for i, oid in enumerate(canonical_history)}; revision_data: dict[str, list[tuple[str, str, str | None]]] = {}
    confirmed: dict[str, dict[str, tuple[str, str | None]]] = {name: {} for name in state["views"]}
    view_closures: dict[str, tuple[set[str], set[str], set[tuple[bytes, str]]]] = {}
    for tx in state["transactions"].values():
        if tx.get("kind") is None and tx["status"] in {"fanout_pending", "published"}:
            for mapping in tx["mappings"]:
                if mapping["canonical"] in confirmed[tx["view"]]: _fail("ANCHOR", tx["view"], "multiple confirmed mappings for canonical commit")
                confirmed[tx["view"]][mapping["canonical"]] = (mapping["submitted"], mapping["change_id"])
    for name, view in sorted(state["views"].items()):
        repo = view_repo(root, name); actual = _history(repo, view["projected_tip"], "PROJECTION", name)
        allowed_commits: set[str] = set(actual)
        allowed_trees: set[str] = set()
        allowed_leaves: set[tuple[bytes, str]] = set()
        expected: list[tuple[str, str, str | None]] = []; previous_visible: dict | None = None; actual_index = 0; previous_projected: str | None = None
        for canonical_oid in canonical_history[:index[view["canonical_frontier"]] + 1]:
            visible = _filtered(_flat(canonical, canonical_oid, "PROJECTION"), view["read"])
            mapping = confirmed[name].get(canonical_oid)
            if mapping: projected, kind, change_id = mapping[0], "submitted", mapping[1]
            elif visible != previous_visible:
                if actual_index >= len(actual): _fail("PROJECTION", name, "missing projected representation")
                projected, kind, change_id = actual[actual_index], "synthetic", parse_change_id(_commit_parts(_raw_commit(canonical, canonical_oid, "PROJECTION", canonical_oid), "PROJECTION", canonical_oid)[3])
            else:
                previous_visible = visible; continue
            expected.append((projected, canonical_oid, kind)); actual_index += 1
            anchor = view["anchors"].get(projected)
            if not anchor or anchor["canonical"] != canonical_oid or anchor["kind"] != kind: _fail("ANCHOR", projected, "anchor does not match canonical replay")
            raw = _raw_commit(repo, projected, "PROJECTION", projected)
            _tree, parents, _lines, _message = _commit_parts(raw, "PROJECTION", projected)
            if parents != ([] if previous_projected is None else [previous_projected]): _fail("PROJECTION", projected, "projected history parent differs")
            if _flat(repo, projected, "PROJECTION") != visible: _fail("PROJECTION", projected, "projected tree differs from canonical filter")
            allowed_trees.add(_tree)
            allowed_trees.update(_no_empty(repo, projected, "PROJECTION"))
            for path, (_mode, _kind, blob_oid) in visible.items():
                allowed_leaves.add((path, blob_oid))
            if kind == "synthetic":
                if set(anchor) != {"canonical", "kind", "change_id"} or anchor["change_id"] != change_id: _fail("ANCHOR", projected, "synthetic anchor has invalid Change-Id")
                _synthetic(raw, projected, canonical_oid, previous_projected, change_id, "PROJECTION")
                if change_id: revision_data.setdefault(change_id, []).append((name, projected, canonical_oid, "synthetic"))
            else:
                if set(anchor) != {"canonical", "kind"}: _fail("ANCHOR", projected, "submitted anchor has invalid shape")
            previous_projected = projected; previous_visible = visible
        if [item[0] for item in expected] != actual or set(view["anchors"]) != set(actual): _fail("ANCHOR", name, "anchors and projected history differ")
        if _flat(repo, view["projected_tip"], "PROJECTION") != _filtered(_flat(canonical, view["canonical_frontier"], "PROJECTION"), view["read"]): _fail("PROJECTION", name, "tip tree differs from frontier filter")
        view_closures[name] = (allowed_commits, allowed_trees, allowed_leaves)
    return revision_data, view_closures


def _plans(root: Path, state: dict, canonical_history: list[str]) -> None:
    canonical = canonical_repo(root); index = {oid: i for i, oid in enumerate(canonical_history)}
    for identifier, job in sorted(state["projection_jobs"].items()):
        if "plan" not in job:
            if job["status"] == "ready": _fail("JOB", identifier, "ready job has no plan")
            continue
        plan = job["plan"]
        needed = {"view", "canonical_ref", "projected_ref", "old_frontier", "new_frontier", "old_tip", "new_tip", "anchors"}
        if set(plan) != needed or job["status"] not in {"running", "ready", "retry"}: _fail("JOB", identifier, "plan is invalid for job state")
        view = state["views"].get(job["view"])
        if not view or any(not isinstance(plan[field], str) for field in needed - {"anchors"}) or not isinstance(plan["anchors"], dict): _fail("JOB", identifier, "plan has invalid shape")
        if plan["view"] != job["view"] or plan["canonical_ref"] != view["canonical_ref"] or plan["projected_ref"] != view["projected_ref"] or plan["old_frontier"] != job.get("expected_frontier") or plan["old_tip"] != job.get("expected_projected_tip") or plan["new_frontier"] != job["canonical_to"] or plan["old_frontier"] != view["canonical_frontier"] or plan["old_tip"] != view["projected_tip"]: _fail("JOB", identifier, "plan cursors do not match job and view")
        if plan["old_frontier"] not in index or plan["new_frontier"] not in index or index[plan["old_frontier"]] > index[plan["new_frontier"]]: _fail("JOB", identifier, "plan frontier range is invalid")
        previous_tip = plan["old_tip"]; previous_visible = _filtered(_flat(canonical, plan["old_frontier"], "JOB"), view["read"]); expected_keys: list[str] = []
        for oid in canonical_history[index[plan["old_frontier"]] + 1:index[plan["new_frontier"]] + 1]:
            visible = _filtered(_flat(canonical, oid, "JOB"), view["read"])
            if visible != previous_visible:
                candidates = [key for key, anchor in plan["anchors"].items() if isinstance(anchor, dict) and anchor.get("canonical") == oid]
                if len(candidates) != 1: _fail("JOB", identifier, "plan anchors do not replay canonically")
                projected = candidates[0]; anchor = plan["anchors"][projected]
                if set(anchor) != {"canonical", "kind", "change_id"} or anchor["kind"] != "synthetic": _fail("JOB", identifier, "plan has non-synthetic anchor")
                canonical_change_id = parse_change_id(
                    _commit_parts(
                        _raw_commit(canonical, oid, "JOB", oid), "JOB", oid
                    )[3]
                )
                if anchor["change_id"] != canonical_change_id:
                    _fail("JOB", identifier, "plan Change-Id differs from canonical commit")
                raw = _raw_commit(view_repo(root, job["view"]), projected, "JOB", projected); _synthetic(raw, projected, oid, previous_tip, anchor["change_id"], "JOB")
                if _flat(view_repo(root, job["view"]), projected, "JOB") != visible: _fail("JOB", identifier, "plan tree differs from filter")
                expected_keys.append(projected); previous_tip = projected
            previous_visible = visible
        if set(expected_keys) != set(plan["anchors"]) or previous_tip != plan["new_tip"]: _fail("JOB", identifier, "plan tip or anchors differ from replay")


def _changes(state: dict, revisions: dict[str, list[tuple[str, str, str | None]]]) -> None:
    # A source publication barrier reserves its Change-Id before the submitted
    # ref and its submitted representation become durable.
    for tx in state["transactions"].values():
        if tx.get("kind") is None and tx["status"] in {"canonical_committed", "source_unconfirmed"}:
            for mapping in tx["mappings"]:
                if mapping["change_id"]:
                    revisions.setdefault(mapping["change_id"], [])
    for tx in state["transactions"].values():
        if tx.get("kind") is None and tx["status"] in {"fanout_pending", "published"}:
            for mapping in tx["mappings"]:
                if mapping["change_id"]: revisions.setdefault(mapping["change_id"], []).append((tx["view"], mapping["submitted"], mapping["canonical"], "submitted"))
    if set(state["changes"]) != set(revisions): _fail("CHANGE", "changes", "logical Change-Id inventory differs from representations")
    active: dict[str, list[dict]] = {}
    for tx in state["transactions"].values():
        if tx.get("kind") is None and tx["status"] in {"canonical_committed", "source_unconfirmed", "fanout_pending", "published"}:
            for mapping in tx["mappings"]:
                if mapping["change_id"]: active.setdefault(mapping["change_id"], []).append(tx)
    for change_id, change in sorted(state["changes"].items()):
        actual: set[tuple] = set()
        for revision in change["revisions"]:
            if not isinstance(revision, dict): _fail("CHANGE", change_id, "revision is not a dictionary")
            required = {"view", "projected", "canonical", "kind"}
            if not required <= set(revision) or revision["kind"] not in {"submitted", "synthetic"}: _fail("CHANGE", change_id, "invalid revision shape")
            if revision["kind"] == "submitted" and revision.get("submitted") != revision["projected"]: _fail("CHANGE", change_id, "submitted revision lacks matching submitted ID")
            if revision["kind"] == "synthetic" and "submitted" in revision: _fail("CHANGE", change_id, "synthetic revision has submitted ID")
            key = (revision["view"], revision["projected"], revision["canonical"], revision["kind"])
            if key in actual: _fail("CHANGE", change_id, "duplicate revision")
            actual.add(key)
        if actual != set(revisions[change_id]): _fail("CHANGE", change_id, "revisions differ from anchors and mappings")
        owners = active.get(change_id, [])
        if owners:
            if change.get("owner") is None or change.get("canonical_ref") is None or any(change["owner"] != tx["principal"] or change["canonical_ref"] != tx["canonical_ref"] for tx in owners): _fail("CHANGE", change_id, "ownership differs from source transaction")
        elif "owner" in change or "canonical_ref" in change:
            _fail("CHANGE", change_id, "unbacked ownership reservation")


def _outbox_jobs(state: dict, canonical_history: list[str]) -> None:
    events_by_tx: dict[str, dict] = {}
    for identifier, event in sorted(state["outbox"].items()):
        tx = state["transactions"].get(event["transaction_id"])
        if not tx or event["id"] != event_id(event["transaction_id"]) or event["type"] != "canonical.committed" or event["sequence"] != tx["sequence"] or event["canonical_to"] != tx["canonical_new"] or len(event["target_views"]) != len(set(event["target_views"])) or any(view not in state["views"] for view in event["target_views"]) or (tx.get("kind") is None and tx["view"] in event["target_views"]) or not set(tx["pending_views"]).issubset(event["target_views"]): _fail("OUTBOX", identifier, "event does not match transaction")
        if event["transaction_id"] in events_by_tx or tx.get("event_id") != identifier: _fail("OUTBOX", identifier, "event identity is not one-to-one")
        events_by_tx[event["transaction_id"]] = event
    for identifier, tx in sorted(state["transactions"].items()):
        event = events_by_tx.get(identifier)
        needs = tx["status"] in ({"canonical_committed", "source_unconfirmed", "fanout_pending", "published"} if tx.get("kind") is None else {"fanout_pending", "published"})
        if tx["status"] == "aborted" or tx["status"] == "prepared": needs = False
        if bool(event) != needs: _fail("OUTBOX", identifier, "transaction event relationship is invalid")
        if event and tx["status"] in {"canonical_committed", "source_unconfirmed"} and event["status"] != "blocked": _fail("OUTBOX", identifier, "unconfirmed source event is not blocked")
        if event and tx["status"] in {"fanout_pending", "published"} and event["status"] == "blocked": _fail("OUTBOX", identifier, "confirmed event remains blocked")
    jobs_by_event: dict[str, list[dict]] = {}
    for identifier, job in sorted(state["projection_jobs"].items()):
        event = state["outbox"].get(job["event_id"]); tx = state["transactions"].get(job["transaction_id"])
        if not event or not tx or event["transaction_id"] != job["transaction_id"] or job["id"] != job_id(job["transaction_id"], job["view"]) or job["event_id"] != event["id"] or job["view"] not in event["target_views"] or job["sequence"] != event["sequence"] or job["sequence"] != tx["sequence"] or job["canonical_to"] != event["canonical_to"] or job["canonical_to"] != tx["canonical_new"]: _fail("JOB", identifier, "job does not match event and transaction")
        if job["status"] in {"running", "ready"} and (not isinstance(job.get("lease_token"), str) or not job["lease_token"] or not isinstance(job.get("lease_expires_at"), (int, float)) or not _oid(job.get("expected_frontier")) or not _oid(job.get("expected_projected_tip"))): _fail("JOB", identifier, "active job lease fields are invalid")
        if job["status"] == "pending" and (job.get("lease_token") is not None or "plan" in job): _fail("JOB", identifier, "pending job has active fields")
        if job["status"] == "retry" and job.get("lease_token") is not None: _fail("JOB", identifier, "retry job retains lease token")
        if job["status"] == "completed" and (job.get("lease_token") is not None or job.get("plan") is not None): _fail("JOB", identifier, "completed job retains active fields")
        jobs_by_event.setdefault(job["event_id"], []).append(job)
    for identifier, event in events_by_tx.items():
        jobs = jobs_by_event.get(event["id"], [])
        if event["status"] in {"blocked", "pending"} and jobs: _fail("OUTBOX", event["id"], "undispatched event has jobs")
        if event["status"] == "dispatched" and {job["view"] for job in jobs} != set(event["target_views"]): _fail("OUTBOX", event["id"], "dispatched event is missing jobs")
        transaction = state["transactions"][identifier]
        if event["status"] == "dispatched":
            incomplete = {job["view"] for job in jobs if job["status"] != "completed"}
            if set(transaction["pending_views"]) != incomplete:
                _fail("JOB", identifier, "pending views do not match dispatched jobs")
            if transaction["status"] == "fanout_pending" and not incomplete:
                _fail("JOB", identifier, "fanout pending transaction has no pending view")
            if transaction["status"] == "published" and incomplete:
                _fail("JOB", identifier, "published transaction has incomplete job")
        if transaction["status"] == "published" and event["target_views"] and event["status"] != "dispatched":
            _fail("OUTBOX", identifier, "published transaction with targets was not dispatched")
        errors = transaction.get("view_errors", {})
        incomplete = {job["view"] for job in jobs if job["status"] != "completed"}
        if not set(errors).issubset(incomplete): _fail("JOB", identifier, "view error is not backed by incomplete job")
    for view in sorted(state["views"]):
        jobs = sorted((job for job in state["projection_jobs"].values() if job["view"] == view), key=lambda item: item["sequence"])
        frontier = state["views"][view]["canonical_frontier"]
        frontier_index = canonical_history.index(frontier)
        for job in jobs:
            if job["status"] == "completed" and canonical_history.index(job["canonical_to"]) > frontier_index:
                _fail("JOB", job["id"], "completed target is ahead of the view frontier")
        if sum(job["status"] in {"running", "ready"} for job in jobs) > 1: _fail("JOB", view, "more than one active job")
        for earlier, later in zip(jobs, jobs[1:]):
            if earlier["status"] != "completed" and later["status"] in {"running", "ready", "completed"}: _fail("JOB", view, "higher sequence passed incomplete work")


def _reachability(root: Path, state: dict, view_closures: dict[str, tuple[set[str], set[str], set[tuple[bytes, str]]]]) -> None:
    for name, view in sorted(state["views"].items()):
        repo = view_repo(root, name)
        allowed_commits, allowed_trees, allowed_leaves = view_closures[name]
        if git(repo, "fsck", "--full", "--strict", "--no-reflogs", "--no-dangling", check=False).returncode: _fail("REACHABILITY", name, "strict fsck failed")
        result = git(repo, "rev-list", "--objects", view["projected_tip"], check=False)
        if result.returncode: _fail("REACHABILITY", name, "cannot enumerate closure")
        for line in result.stdout.splitlines():
            parts = line.split(b" ", 1)
            oid = parts[0].decode(errors="replace")
            path = parts[1] if len(parts) > 1 else None
            kind = git(repo, "cat-file", "-t", oid, check=False)
            if kind.returncode: _fail("REACHABILITY", oid, "missing local object")
            kind_str = kind.stdout.strip()
            if kind_str == b"commit":
                if path is not None:
                    _fail("REACHABILITY", oid, "commit appears with a path")
                if oid not in allowed_commits:
                    _fail("REACHABILITY", oid, "reachable commit is not in anchored projected chain")
            elif kind_str == b"tree":
                if oid not in allowed_trees:
                    _fail("REACHABILITY", oid, "reachable tree is not in projected snapshot")
            elif kind_str == b"blob":
                if path is None:
                    _fail("REACHABILITY", oid, "reachable blob has no path")
                if (path, oid) not in allowed_leaves:
                    _fail("REACHABILITY", _path(path), "reachable leaf is not an authorized blob")
            else:
                _fail("REACHABILITY", oid, "unsupported reachable object type")


def _phase(code: str, function: Any) -> Any:
    try:
        return function()
    except ForgeError as error:
        if str(error).startswith("invariant "):
            raise
        _fail(code, "forge", str(error))
    except (KeyError, TypeError, ValueError, OSError, UnicodeError) as error:
        _fail(code, "forge", str(error))


def check_invariants(root: Path) -> None:
    """Raise ForgeError if the forge violates a global invariant."""
    lock_path = root / "state.lock"
    try:
        with lock_path.open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = copy.deepcopy(load_state(root))
            _phase("STATE_SCHEMA", lambda: _schema(state))
            canonical_refs, view_refs = _phase("FORGE_LAYOUT", lambda: _layout_and_refs(root, state))
    except ForgeError:
        raise
    except OSError as error:
        _fail("STATE_SCHEMA", "state", f"cannot acquire snapshot: {error}")
    tip = _phase("CANONICAL_REF", lambda: _check_refs(root, state, canonical_refs, view_refs))
    canonical_history = _phase("CANONICAL_GRAPH", lambda: _canonical_graph(root, tip))
    _phase("FRONTIER", lambda: _policy_frontiers(root, state, canonical_history, tip))
    _phase("MAPPING", lambda: _source_transactions(root, state, canonical_history, tip))
    _phase("CANONICAL_REF", lambda: _admin_transactions(root, state, canonical_history, tip))
    revisions, view_closures = _phase("PROJECTION", lambda: _projected(root, state, canonical_history))
    _phase("JOB", lambda: _plans(root, state, canonical_history))
    _phase("CHANGE", lambda: _changes(state, revisions))
    _phase("OUTBOX", lambda: _outbox_jobs(state, canonical_history))
    _phase("REACHABILITY", lambda: _reachability(root, state, view_closures))
