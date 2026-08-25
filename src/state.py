"""Control-plane persistence, leases, and WAL-derived indexes."""

from __future__ import annotations

import contextlib
import copy
import threading
import time
import uuid
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


def save_state(root: Path, state: dict) -> None:
    session = _session_for(root)
    if session is not None:
        leases = [(scope, session["owner"]) for scope in session["scopes"]]
        session["store"].save_document(
            state, leases, root, baseline=session.get("baseline")
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


def recover(root: Path, state: dict) -> None:
    """Refresh the database indexes from committed repository records."""
    if state.get("repository_state") != "ready":
        raise ForgeError("forge initialization is incomplete")
    from repository import Repository

    repository = Repository.open(root)
    manifest, _version, replay = repository.load()
    if state.get("repository_id") != repository.repository_id:
        raise ForgeError("control-plane repository ID differs from manifest", "integrity")
    before = {
        "transactions": state.get("transactions"),
        "changes": state.get("changes"),
        "views": state.get("views"),
        "last_indexed_seq": state.get("last_indexed_seq"),
    }
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


@contextlib.contextmanager
def locked(
    root: Path,
    recoverer: Callable[[Path, dict], None] = recover,
    scopes: tuple[str, ...] = ("global",),
) -> Iterator[dict]:
    resolved = str(root.resolve())
    if _sessions.active.get(resolved) is not None:
        raise ForgeError("forge session is already active in this thread")
    from store import process_identity

    store = open_store(root)
    owner = f"{uuid.uuid4().hex}:{process_identity()}"
    store.acquire_leases(list(scopes), owner)
    session = {
        "store": store,
        "owner": owner,
        "scopes": list(scopes),
        "baseline": None,
    }
    _sessions.active[resolved] = session
    try:
        state = store.load_document()
        recoverer(root, state)
        session["baseline"] = copy.deepcopy(state)
        yield state
    finally:
        _sessions.active.pop(resolved, None)
        try:
            store.release_leases(list(scopes), owner)
        finally:
            store.close()


def heartbeat_session(root: Path, token: str, seconds: float | None = None) -> float:
    """Extend every lease owned by ``token`` and return the new expiry."""
    store = open_store(root)
    try:
        expiry = time.time() + (seconds or LEASE_TTL_SECONDS)
        scopes = store.scopes_for_owner(token)
        if not scopes:
            raise ForgeError("no active storage lease carries this token")
        for scope in scopes:
            store.heartbeat_lease(scope, token, expiry)
        return expiry
    finally:
        store.close()
