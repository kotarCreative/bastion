"""Durable state, locking, and cross-repository transaction recovery."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
from typing import Callable, Iterator

from errors import ForgeError
from failpoints import checkpoint
from git import git, ref
from identity import require_matching_descriptor
from layout import ZERO_OID, canonical_repo, view_repo


def state_path(root: Path) -> Path:
    return root / "state.json"


def load_state(root: Path) -> dict:
    try:
        return json.loads(state_path(root).read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise ForgeError(f"cannot read forge state: {error}") from error


def save_state(root: Path, state: dict) -> None:
    target = state_path(root)
    temporary = target.with_suffix(".tmp")
    with temporary.open("w") as output_file:
        output_file.write(json.dumps(state, indent=2, sort_keys=True) + "\n")
        output_file.flush()
        os.fsync(output_file.fileno())
    checkpoint(root, "state.temporary_fsynced")
    os.replace(temporary, target)
    checkpoint(root, "state.replaced")
    directory = os.open(root, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    checkpoint(root, "state.directory_fsynced")


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


@contextlib.contextmanager
def locked(
    root: Path, recoverer: Callable[[Path, dict], None] = recover
) -> Iterator[dict]:
    with (root / "state.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = load_state(root)
        version = state.get("version")
        if version != 2:
            if version == 1:
                raise ForgeError("forge state version 1 requires migrate-identities")
            raise ForgeError(f"unsupported forge state version: {version!r}")
        recoverer(root, state)
        yield state
