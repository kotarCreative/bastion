"""Receive-pack orchestration around the testable forge components."""

from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path

import identity
from errors import ForgeError
from failpoints import checkpoint
from git import (
    clean_env,
    copy_submitted_closure,
    git,
    ref,
    submitted_commits,
    tree_oid,
    verify_no_extra_objects,
)
from layout import canonical_repo, view_repo
from projection import visible_advance
from reconciliation import reconcile_commits
from state import (
    locked,
    reserve_change_ids,
    save_state,
    validate_change_ids,
    write_receiver_marker,
)
from validation import parse_updates, validate_ref_update


def transaction_id(name: str, old: str, new: str) -> str:
    return hashlib.sha256(f"{name}\0{old}\0{new}".encode()).hexdigest()


def candidate_environment(canonical: Path, directory: Path) -> dict[str, str]:
    env = clean_env()
    env["GIT_OBJECT_DIRECTORY"] = str(directory)
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str(canonical / "objects")
    return env


def promote_objects(source: Path, canonical: Path) -> None:
    for object_path in source.rglob("*"):
        if not object_path.is_file():
            continue
        relative = object_path.relative_to(source)
        target = canonical / "objects" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            os.link(object_path, target)


def prepare_submission(
    root: Path,
    name: str,
    view: dict,
    descriptor: dict,
    old: str,
    new: str,
    hook_env: dict[str, str],
) -> tuple[str, str, list[dict[str, str | None]], Path]:
    destination = view_repo(root, name)
    canonical = canonical_repo(root)
    commits = submitted_commits(destination, old, new, hook_env)
    verify_no_extra_objects(destination, new, hook_env)
    canonical_old = ref(canonical, view["canonical_ref"])
    if git(
        canonical,
        "merge-base",
        "--is-ancestor",
        view["canonical_frontier"],
        canonical_old,
        check=False,
    ).returncode:
        raise ForgeError("canonical history no longer contains the view frontier")
    visible_advance(
        canonical,
        view["canonical_frontier"],
        canonical_old,
        destination,
        descriptor,
        tree_oid(destination, old, hook_env),
    )

    transaction_objects = root / "transaction-objects" / transaction_id(name, old, new)
    shutil.rmtree(transaction_objects, ignore_errors=True)
    transaction_objects.mkdir(parents=True)
    candidate_env = candidate_environment(canonical, transaction_objects)
    try:
        canonical_new, mappings = reconcile_commits(
            canonical,
            destination,
            commits,
            canonical_old,
            old,
            descriptor,
            hook_env,
            candidate_env,
        )
        copy_submitted_closure(destination, old, new, hook_env)
    except Exception:
        shutil.rmtree(transaction_objects, ignore_errors=True)
        raise
    return canonical_old, canonical_new, mappings, transaction_objects


def pre_receive(root: Path, name: str, updates: bytes) -> None:
    parsed = parse_updates(updates)
    with locked(root) as state:
        view = state["views"].get(name)
        if not view:
            raise ForgeError("exactly one authorized branch may be updated")
        descriptor = identity.descriptor_for_view(state, name)
        from outbox import view_has_unfinished_jobs
        from state import has_unconfirmed_source, source_publication_pending

        old, new, _updated_ref = validate_ref_update(view, parsed)
        retry_id = transaction_id(name, old, new)
        retry = state["transactions"].get(retry_id)
        if retry and retry["status"] == "source_unconfirmed":
            identity.require_matching_descriptor(
                view, retry["descriptor_id"], retry["descriptor_generation"]
            )
            if retry.get("principal_id") != view["principal_id"]:
                raise ForgeError("projection descriptor changed")
            write_receiver_marker(root, retry_id, os.getppid())
            retry.pop("source_error", None)
            save_state(root, state)
            return
        if source_publication_pending(root, state, name):
            raise ForgeError("view has an active receive transaction")
        if has_unconfirmed_source(state):
            raise ForgeError("canonical history contains an unconfirmed source publication")
        if view_has_unfinished_jobs(state, name):
            raise ForgeError("view has pending projection work; retry after synchronization")
        hook_env = os.environ.copy()
        canonical_old, canonical_new, mappings, transaction_objects = prepare_submission(
            root, name, view, descriptor, old, new, hook_env
        )
        try:
            validate_change_ids(
                state, view["principal_id"], view["canonical_ref"], mappings
            )
            promote_objects(transaction_objects, canonical_repo(root))
        finally:
            shutil.rmtree(transaction_objects, ignore_errors=True)
        checkpoint(root, "source.validation_complete")
        txid = transaction_id(name, old, new)
        sequence = state.setdefault("next_transaction_sequence", 1)
        state["next_transaction_sequence"] = sequence + 1
        transaction = {
            "id": txid,
            "view": name,
            "principal_id": view["principal_id"],
            "descriptor_id": descriptor["id"],
            "descriptor_generation": view["descriptor_generation"],
            "target_descriptors": {
                other_name: {
                    "descriptor_id": other["descriptor_id"],
                    "descriptor_generation": other["descriptor_generation"],
                }
                for other_name, other in sorted(state["views"].items())
                if other_name != name
            },
            "canonical_ref": view["canonical_ref"],
            "projected_ref": view["projected_ref"],
            "canonical_frontier_old": view["canonical_frontier"],
            "canonical_old": canonical_old,
            "canonical_new": canonical_new,
            "projected_old": old,
            "projected_new": new,
            "mappings": mappings,
            "sequence": sequence,
            "pending_views": sorted(
                view_name for view_name in state["views"] if view_name != name
            ),
            "status": "prepared",
        }
        write_receiver_marker(root, txid, os.getppid())
        state["transactions"][txid] = transaction
        save_state(root, state)
        checkpoint(root, "source.prepared_persisted")
        from service import update_canonical_ref_locked

        update_canonical_ref_locked(
            root, state, canonical_old, canonical_new, create_event=False
        )
        checkpoint(root, "source.canonical_ref_published")
        reserve_change_ids(
            state, view["principal_id"], view["canonical_ref"], mappings
        )
        transaction["status"] = "canonical_committed"
        from outbox import enqueue_canonical_committed

        enqueue_canonical_committed(state, transaction)
        save_state(root, state)
        checkpoint(root, "source.canonical_state_persisted")


def post_receive(root: Path, name: str, updates: bytes) -> None:
    checkpoint(root, "source.ref_published")
    parsed = parse_updates(updates)
    with locked(root) as state:
        for old, new, updated_ref in parsed:
            txid = transaction_id(name, old, new)
            transaction = state["transactions"].get(txid)
            if transaction and transaction["projected_ref"] == updated_ref:
                (root / "receivers" / txid).unlink(missing_ok=True)
        save_state(root, state)
        checkpoint(root, "source.receiver_cleanup_persisted")
    checkpoint(root, "source.confirmation_persisted")
    from worker import drain_projection_jobs

    drain_projection_jobs(root)
