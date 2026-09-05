"""Control-plane persistence, leases, and WAL-derived indexes."""

from __future__ import annotations

import contextlib
import copy
import threading
import time
from pathlib import Path
from typing import Callable, Iterator

from errors import ForgeError
from git import git
from store import LEASE_TTL_SECONDS, open_store


class _Sessions(threading.local):
    def __init__(self) -> None:
        self.active: dict[str, dict] = {}


_sessions = _Sessions()


def _session_for(root: Path) -> dict | None:
    return _sessions.active.get(str(root.resolve()))


def load_state(root: Path) -> dict:
    store = open_store(root)
    try:
        return store.load_document()
    finally:
        store.close()


def load_state_sections(root: Path, sections: list[str]) -> dict:
    """Load forge_meta plus only the requested control-plane sections."""
    store = open_store(root)
    try:
        return store.load_sections(sections)
    finally:
        store.close()


def load_state_meta(root: Path) -> dict:
    """Load only the forge_meta row (readiness fence) without any tables."""
    return load_state_sections(root, [])


def load_state_row(root: Path, section: str, key: str) -> dict | None:
    """Fetch one checksummed control-plane row by primary key."""
    store = open_store(root)
    try:
        return store.load_row(section, key)
    finally:
        store.close()


def save_state(root: Path, state: dict) -> None:
    session = _session_for(root)
    if session is not None:
        leases = [(scope, session["owner"]) for scope in session["scopes"]]
        session["store"].save_document(
            state,
            leases,
            root,
            baseline=session.get("baseline"),
            baseline_versions=session.get("baseline_versions"),
        )
        return
    store = open_store(root)
    try:
        store.save_document(state, [], root)
    finally:
        store.close()


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return git(
        repo,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        check=False,
    ).returncode == 0


def process_start_time(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
    except OSError:
        return None
    return fields[21] if len(fields) > 21 else None


def recover(
    root: Path,
    state: dict,
    *,
    pinned: tuple | None = None,
    repository: object | None = None,
) -> None:
    """Refresh the database indexes from committed repository records.

    Incremental: read the repository fence and ``last_indexed_seq``, pin the
    current repository snapshot, fail if the database sequence is ahead of
    repository truth, do no WAL replay and no table rebuild when the indexed
    sequence already matches, and otherwise merge only the suffix records
    newer than the indexed sequence into the existing derived rows plus the
    sequence atomically (via the caller's locked session). An explicit
    repair path (``repair_derived_indexes``) may discard and fully
    reconstruct derived rows; routine ``locked()`` entry never does that
    reconstruction when already current.
    """
    if state.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    from repository import Repository

    repository = repository or Repository.open(root)
    assert isinstance(repository, Repository)
    if pinned is not None:
        manifest, _version, replay = pinned
    else:
        manifest, _version, replay = repository.load()
    if state.get("repository_id") != repository.repository_id:
        raise ForgeError("control-plane repository ID differs from manifest", "integrity")
    indexed = state.get("last_indexed_seq", 0)
    if not isinstance(indexed, int) or isinstance(indexed, bool):
        raise ForgeError("control-plane index sequence is invalid", "integrity")
    if indexed > manifest.head_seq:
        raise ForgeError("control-plane index is ahead of repository truth", "integrity")
    if indexed == manifest.head_seq:
        return
    before = {
        "transactions": state.get("transactions"),
        "changes": state.get("changes"),
        "views": state.get("views"),
        "last_indexed_seq": state.get("last_indexed_seq"),
    }
    # Incremental suffix: only records newer than the indexed sequence are
    # merged. Existing derived rows are preserved; this keeps one-record
    # recovery bounded by the suffix, not by complete history or table size
    # (saving is separately scoped to changed keys in Store._save_table).
    new_records = [record for record in replay.records if record["seq"] > indexed]
    if not new_records and indexed != manifest.head_seq:
        # Suffix missing despite sequence lag (e.g. truncated replay):
        # fall back to a full rebuild rather than advancing the sequence
        # with stale derived rows.
        new_records = list(replay.records)
        state["transactions"] = {}
        state["changes"] = {}
    if (
        len(new_records) < len(replay.records)
        and isinstance(state.get("transactions"), dict)
        and isinstance(state.get("changes"), dict)
    ):
        transactions = dict(state.get("transactions") or {})
        for record in new_records:
            transactions[record["event_id"]] = {
                "event_id": record["event_id"],
                "seq": record["seq"],
                "kind": record["kind"],
                "committed": True,
            }
        state["transactions"] = transactions
        changes = dict(state.get("changes") or {})
        for change_id, claim in replay.change_claims.items():
            if change_id not in changes:
                changes[change_id] = {
                    "owner_principal_id": claim["owner_principal_id"],
                    "canonical_ref": claim["canonical_ref"],
                    "revisions": [],
                }
        state["changes"] = changes
    else:
        state["transactions"] = {
            record["event_id"]: {
                "event_id": record["event_id"],
                "seq": record["seq"],
                "kind": record["kind"],
                "committed": True,
            }
            for record in replay.records
        }
        state["changes"] = {
            change_id: {
                "owner_principal_id": claim["owner_principal_id"],
                "canonical_ref": claim["canonical_ref"],
                "revisions": [],
            }
            for change_id, claim in replay.change_claims.items()
        }
    previous_views = state.get("views", {})
    state["views"] = {
        view_id: {
            "principal_id": active["principal_id"],
            "projected_ref": active["projected_ref"],
            "descriptor_id": active["descriptor_id"],
            "descriptor_generation": active["descriptor_generation"],
            "projection_epoch": active["projection_epoch"],
            "canonical_frontier": active["canonical_frontier"],
            "indexed_seq": manifest.head_seq,
            "materialized_seq": previous_views.get(view_id, {}).get("materialized_seq", 0),
            "projected_tip": previous_views.get(view_id, {}).get("projected_tip"),
            "last_materialization_error": previous_views.get(view_id, {}).get(
                "last_materialization_error"
            ),
        }
        for view_id, active in replay.active_views.items()
    }
    state["last_indexed_seq"] = manifest.head_seq
    after = {key: state.get(key) for key in before}
    if after != before:
        save_state(root, state)


def audit_event(
    root: Path, kind: str, subject: str, detail: str, event_id: str | None = None
) -> None:
    session = _session_for(root)
    if session is not None:
        session["store"].audit(kind, subject, detail, event_id)
        return
    store = open_store(root)
    try:
        store.audit(kind, subject, detail, event_id)
    finally:
        store.close()


def repair_derived_indexes(root: Path) -> None:
    """Explicit repair: discard and fully reconstruct derived rows from WAL."""
    with locked(root) as state:
        state["transactions"] = {}
        state["changes"] = {}
        state["views"] = {}
        state["last_indexed_seq"] = 0
        recover(root, state)
        save_state(root, state)


@contextlib.contextmanager
def locked(
    root: Path,
    recoverer: Callable[[Path, dict], None] = recover,
    scopes: tuple[str, ...] = ("global",),
) -> Iterator[dict]:
    resolved = str(root.resolve())
    if _sessions.active.get(resolved) is not None:
        raise ForgeError("forge session is already active in this thread")
    from store import owner_identity

    store = open_store(root)
    owner = owner_identity(root)
    store.acquire_leases(list(scopes), owner, root)
    session = {
        "store": store,
        "owner": owner,
        "scopes": list(scopes),
        "baseline": None,
        "baseline_versions": None,
    }
    _sessions.active[resolved] = session
    try:
        try:
            state, versions = store.load_document_with_versions()
        except AttributeError:
            state = store.load_document()
            versions = None
        recoverer(root, state)
        session["baseline"] = copy.deepcopy(state)
        session["baseline_versions"] = copy.deepcopy(versions) if versions is not None else None
        yield state
    finally:
        _sessions.active.pop(resolved, None)
        try:
            store.release_leases(list(scopes), owner)
        finally:
            store.close()


def heartbeat_session(
    root: Path,
    token: str,
    seconds: float | None = None,
    scopes: list[str] | None = None,
) -> float:
    """Extend the exact expected lease set and return the committed expiry.

    ``scopes`` is the exact expected scope set (CLI ``--scope`` values or an
    in-process session's acquired set). The heartbeat verifies and updates
    the exact count in one explicit transaction and commits before returning
    the new expiry; a second connection observes the persisted expiry after
    the caller closes. If any expected scope is absent or has a different
    owner, every update rolls back and lease loss is reported.
    """
    store = open_store(root)
    try:
        expiry = time.time() + (seconds or LEASE_TTL_SECONDS)
        expected = list(scopes) if scopes is not None else store.scopes_for_owner(token)
        if not expected:
            raise ForgeError("no active storage lease carries this token")
        store.heartbeat_leases(expected, token, expiry)
        return expiry
    finally:
        store.close()
