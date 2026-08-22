"""Durable state, session leases, and cross-repository transaction recovery.

State persistence is delegated to `store.py` (`.specs/009-production-
transaction-storage.md`): short optimistic transactions against SQLite or
PostgreSQL, lease-based session exclusion instead of a filesystem lock, and
checksum-verified document loads.
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Iterator

from errors import ForgeError
from failpoints import checkpoint
from git import git, ref
from identity import require_matching_descriptor
from layout import ZERO_OID, canonical_repo, view_repo
from store import LEASE_TTL_SECONDS, backend_record_path, open_store


class _Sessions(threading.local):
    def __init__(self) -> None:
        self.active: dict[str, dict] = {}


_sessions = _Sessions()


def _session_for(root: Path) -> dict | None:
    return _sessions.active.get(str(root.resolve()))


def state_path(root: Path) -> Path:
    """Legacy location of the pre-storage JSON document (imports only)."""
    return root / "state.json"


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
    if not backend_record_path(root).exists():
        from store import database_uri, initialize_store

        uri = database_uri(root)
        if uri.startswith("postgres://") or uri.startswith("postgresql://"):
            raise ForgeError("a postgres forge must be created by init or migration")
        initialize_store(root).close()
    store = open_store(root)
    try:
        store.save_document(state, [], root)
    finally:
        store.close()


def apply_transaction_state(state: dict, transaction: dict) -> None:
    if transaction.get("principal_id"):
        reserve_change_ids(
            state,
            transaction["principal_id"],
            transaction["canonical_ref"],
            transaction["mappings"],
        )
    view = state["views"][transaction["view"]]
    require_matching_descriptor(
        view, transaction["descriptor_id"], transaction["descriptor_generation"]
    )
    if view["canonical_frontier"] == transaction.get(
        "canonical_frontier_old",
        transaction.get("canonical_old", view["canonical_frontier"]),
    ):
        view["canonical_frontier"] = transaction["canonical_new"]
    if view["projected_tip"] == transaction.get(
        "projected_old", view["projected_tip"]
    ):
        view["projected_tip"] = transaction["projected_new"]
    for mapping in transaction["mappings"]:
        view["anchors"][mapping["submitted"]] = {
            "canonical": mapping["canonical"],
            "kind": "submitted",
            "descriptor_id": transaction["descriptor_id"],
            "descriptor_generation": transaction["descriptor_generation"],
        }
        change_id = mapping["change_id"]
        if not change_id:
            continue
        record_change_representation(
            state,
            change_id,
            transaction["view"],
            mapping["submitted"],
            mapping["canonical"],
            "submitted",
            transaction["descriptor_id"],
            transaction["descriptor_generation"],
        )


def record_change_representation(
    state: dict,
    change_id: str,
    view: str,
    projected: str,
    canonical: str,
    kind: str,
    descriptor_id: str,
    descriptor_generation: int,
) -> None:
    change = state["changes"].setdefault(change_id, {"revisions": []})
    revision = {
        "view": view,
        "projected": projected,
        "canonical": canonical,
        "kind": kind,
        "descriptor_id": descriptor_id,
        "descriptor_generation": descriptor_generation,
    }
    if kind == "submitted":
        revision["submitted"] = projected
    if revision not in change["revisions"]:
        change["revisions"].append(revision)


def reserve_change_ids(
    state: dict,
    principal_id: str,
    canonical_ref: str,
    mappings: list[dict[str, str | None]],
) -> None:
    validate_change_ids(state, principal_id, canonical_ref, mappings)
    for mapping in mappings:
        change_id = mapping["change_id"]
        if not change_id:
            continue
        existing = state["changes"].get(change_id)
        if not existing:
            state["changes"][change_id] = {
                "owner_principal_id": principal_id,
                "canonical_ref": canonical_ref,
                "revisions": [],
            }


def validate_change_ids(
    state: dict,
    principal_id: str,
    canonical_ref: str,
    mappings: list[dict[str, str | None]],
) -> None:
    for mapping in mappings:
        change_id = mapping["change_id"]
        if not change_id:
            continue
        existing = state["changes"].get(change_id)
        if existing and (
            existing.get("owner_principal_id") != principal_id
            or existing.get("canonical_ref") != canonical_ref
        ):
            raise ForgeError("Change-Id belongs to another principal or branch")


def receiver_active(root: Path, transaction_id: str) -> bool:
    receiver = root / "receivers" / transaction_id
    if not receiver.exists():
        return False
    try:
        pid_text, expected_start = receiver.read_text().split()
        pid = int(pid_text)
        os.kill(pid, 0)
        if process_start_time(pid) != expected_start:
            receiver.unlink(missing_ok=True)
            return False
        return True
    except (OSError, ValueError):
        receiver.unlink(missing_ok=True)
        return False


def process_start_time(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
    except OSError:
        return None
    return fields[21] if len(fields) > 21 else None


def write_receiver_marker(root: Path, transaction_id: str, pid: int) -> None:
    start = process_start_time(pid)
    if not start:
        return
    receiver = root / "receivers" / transaction_id
    receiver.parent.mkdir(exist_ok=True)
    receiver.write_text(f"{pid} {start}")


def is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    return (
        git(
            repo,
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        ).returncode
        == 0
    )


def source_publication_pending(root: Path, state: dict, view_name: str) -> bool:
    destination = view_repo(root, view_name)
    for transaction in state.get("transactions", {}).values():
        if transaction.get("view") != view_name or transaction["status"] not in {
            "prepared",
            "canonical_committed",
            "source_unconfirmed",
        }:
            continue
        actual = ref(destination, transaction["projected_ref"])
        if actual == ZERO_OID or not is_ancestor(
            destination, transaction["projected_new"], actual
        ):
            return True
    return False


def has_unconfirmed_source(state: dict) -> bool:
    return any(
        transaction.get("kind") != "canonical_update"
        and transaction["status"]
        in {"prepared", "canonical_committed", "source_unconfirmed"}
        for transaction in state.get("transactions", {}).values()
    )


def recover(root: Path, state: dict) -> None:
    from outbox import dispatch_outbox, enqueue_canonical_committed

    changed = False
    transactions = sorted(
        state.get("transactions", {}).values(),
        key=lambda transaction: (transaction.get("sequence", 0), transaction["id"]),
    )
    for transaction in transactions:
        if transaction["status"] == "published":
            continue
        canonical = canonical_repo(root)
        if transaction.get("kind") == "canonical_update":
            canonical_tip = ref(canonical, transaction["canonical_ref"])
            contains_new = canonical_tip == transaction["canonical_new"] or (
                transaction["canonical_new"] != ZERO_OID
                and is_ancestor(canonical, transaction["canonical_new"], canonical_tip)
            )
            if transaction["status"] == "prepared" and contains_new:
                enqueue_canonical_committed(state, transaction, ready=True)
                transaction["status"] = (
                    "fanout_pending" if transaction["pending_views"] else "published"
                )
                changed = True
            elif transaction["status"] == "prepared":
                transaction["status"] = "aborted"
                transaction["abort_reason"] = (
                    "canonical administrative update was not committed"
                )
                changed = True
            continue
        if transaction["status"] == "fanout_pending":
            continue
        destination = view_repo(root, transaction["view"])
        canonical_tip = ref(canonical, transaction["canonical_ref"])
        projected_tip = ref(destination, transaction["projected_ref"])
        contains_new = canonical_tip == transaction["canonical_new"] or (
            transaction["canonical_new"] != ZERO_OID
            and is_ancestor(canonical, transaction["canonical_new"], canonical_tip)
        )
        if contains_new and transaction["status"] == "prepared":
            if transaction.get("principal_id"):
                reserve_change_ids(
                    state,
                    transaction["principal_id"],
                    transaction["canonical_ref"],
                    transaction["mappings"],
                )
            transaction["status"] = "canonical_committed"
            enqueue_canonical_committed(state, transaction)
            changed = True
        elif transaction["status"] == "prepared":
            transaction["status"] = "aborted"
            transaction["abort_reason"] = (
                "canonical compare-and-swap did not publish this transaction"
            )
            (root / "receivers" / transaction["id"]).unlink(missing_ok=True)
            changed = True
            continue
        if transaction["status"] in {"canonical_committed", "source_unconfirmed"}:
            source_published = projected_tip != ZERO_OID and is_ancestor(
                destination, transaction["projected_new"], projected_tip
            )
            if source_published:
                apply_transaction_state(state, transaction)
                enqueue_canonical_committed(state, transaction, ready=True)
                transaction["status"] = "fanout_pending"
                transaction.pop("source_error", None)
                (root / "receivers" / transaction["id"]).unlink(missing_ok=True)
                changed = True
            elif not receiver_active(root, transaction["id"]):
                transaction["status"] = "source_unconfirmed"
                transaction["source_error"] = (
                    "canonical commit accepted but source ref publication was not confirmed"
                )
                changed = True
        if transaction["status"] == "fanout_pending" and not transaction.get(
            "pending_views"
        ):
            transaction["status"] = "published"
            changed = True
    receivers = root / "receivers"
    for marker in receivers.glob("*") if receivers.exists() else ():
        txid = marker.name
        tx = state.get("transactions", {}).get(txid)
        if tx and tx["status"] in {"aborted", "fanout_pending", "published"}:
            marker.unlink(missing_ok=True)
            changed = True

    before = json.dumps(
        (state.get("outbox", {}), state.get("projection_jobs", {})), sort_keys=True
    )
    dispatch_outbox(state)
    changed = changed or before != json.dumps(
        (state.get("outbox", {}), state.get("projection_jobs", {})), sort_keys=True
    )
    if changed:
        save_state(root, state)
        checkpoint(root, "recovery.state_persisted")


def idempotency_lookup(root: Path, key: str) -> dict | None:
    session = _session_for(root)
    if session is not None:
        return session["store"].lookup_idempotency(key)
    store = open_store(root)
    try:
        return store.lookup_idempotency(key)
    finally:
        store.close()


def idempotency_record(root: Path, key: str, fingerprint: dict, outcome: dict) -> None:
    session = _session_for(root)
    if session is not None:
        session["store"].record_idempotency(key, fingerprint, outcome)
        return
    store = open_store(root)
    try:
        store.record_idempotency(key, fingerprint, outcome)
    finally:
        store.close()


def audit_event(root: Path, kind: str, subject: str, detail: str) -> None:
    session = _session_for(root)
    if session is not None:
        session["store"].audit(kind, subject, detail)
        return
    store = open_store(root)
    try:
        store.audit(kind, subject, detail)
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
        # Deletion diffing starts from what this session observed after
        # recovery, so concurrent rows from other scopes are never deleted.
        session["baseline"] = copy.deepcopy(state)
        yield state
    finally:
        _sessions.active.pop(resolved, None)
        try:
            store.release_leases(list(scopes), owner)
        finally:
            store.close()


def heartbeat_session(root: Path, token: str, seconds: float | None = None) -> float:
    """Extend every lease owned by ``token``; returns the new expiry."""
    session = _session_for(root)
    store = open_store(root)
    try:
        expiry = time.time() + (seconds or LEASE_TTL_SECONDS)
        scopes = store.scopes_for_owner(token)
        if not scopes:
            raise ForgeError("no active storage lease carries this token")
        for scope in scopes:
            store.heartbeat_lease(scope, token, expiry)
        if session is not None and session["owner"] == token:
            session["expires_at"] = expiry
        return expiry
    finally:
        store.close()


def require_quiescent(root: Path, state: dict) -> None:
    """Require global quiescence: no unfinished work anywhere in the forge."""
    for identifier, transaction in sorted(state["transactions"].items()):
        if transaction["status"] not in {"published", "aborted"}:
            raise ForgeError(f"forge has unfinished transactions; this operation requires global quiescence: {identifier}")
        if transaction["status"] == "published" and transaction.get("pending_views"):
            raise ForgeError(f"published transaction retains pending views: {identifier}")
        if "view_errors" in transaction and transaction["view_errors"]:
            raise ForgeError(f"transaction retains view errors: {identifier}")
    for identifier, event in sorted(state["outbox"].items()):
        if event["status"] != "dispatched":
            raise ForgeError(f"forge has undispatched events: {identifier}")
    for identifier, job in sorted(state["projection_jobs"].items()):
        if job["status"] != "completed":
            raise ForgeError(f"forge has unfinished projection jobs: {identifier}")
        if job.get("lease_token") is not None or "lease_expires_at" in job or "plan" in job:
            raise ForgeError(f"completed job retains active fields: {identifier}")
    receivers = root / "receivers"
    if receivers.exists() and any(receivers.iterdir()):
        raise ForgeError("forge has receiver markers")
    from layout import canonical_repo, view_repo
    from git import ref as git_ref

    for name, view in sorted(state["views"].items()):
        actual = git_ref(view_repo(root, name), view["projected_ref"])
        if actual != view["projected_tip"]:
            raise ForgeError(f"view ref differs from its persisted cursor: {name}")
    actual = git_ref(canonical_repo(root), f"refs/heads/{state['canonical_branch']}")
    if actual == ZERO_OID:
        raise ForgeError("canonical branch is missing")
