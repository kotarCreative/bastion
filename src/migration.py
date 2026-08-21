"""Explicit, quiescent version-1 to version-2 identity migration."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path

import identity
from errors import ForgeError
from invariants import check_invariants, check_loaded, collect_layout
from state import load_state, locked_unversioned, save_state


def _legacy_role_id(view_name: str) -> str:
    return hashlib.sha256(view_name.encode()).hexdigest()


def _require_quiescent(root: Path, state: dict) -> None:
    for identifier, transaction in sorted(state["transactions"].items()):
        if transaction["status"] not in {"published", "aborted"}:
            raise ForgeError(f"forge has unfinished transactions; migrate-identities requires quiescence: {identifier}")
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
        raise ForgeError("forge has receiver markers; migration requires quiescence")
    canonical_name = f"refs/heads/{state['canonical_branch']}"
    from layout import canonical_repo, view_repo
    from git import ref as git_ref

    for name, view in sorted(state["views"].items()):
        actual = git_ref(view_repo(root, name), view["projected_ref"])
        if actual != view["projected_tip"]:
            raise ForgeError(f"view ref differs from its persisted cursor: {name}")
    actual = git_ref(canonical_repo(root), canonical_name)
    from layout import ZERO_OID

    if actual == ZERO_OID:
        raise ForgeError("canonical branch is missing")


def _build_candidate(state: dict) -> dict:
    candidate = copy.deepcopy(state)
    candidate.update(
        {
            "principals": {},
            "teams": {},
            "roles": {},
            "role_bindings": {},
            "view_descriptors": {},
        }
    )
    canonical_ref = f"refs/heads/{state['canonical_branch']}"
    assignments: dict[str, tuple[str, int]] = {}
    for view_name, view in sorted(candidate["views"].items()):
        principal_id = view["principal"]
        read = list(view["read"])
        write = list(view["write"])
        role_id = _legacy_role_id(view_name)
        if principal_id not in candidate["principals"]:
            identity.add_principal(candidate, principal_id)
        normalized_read = identity.minimize_prefixes(read)
        normalized_write = identity.minimize_prefixes(write)
        if not normalized_read and not normalized_write:
            raise ForgeError(f"legacy view has no readable paths: {view_name}")
        candidate["roles"][role_id] = {
            "id": role_id,
            "read": normalized_read,
            "write": normalized_write,
        }
        binding_id = identity.binding_identifier("principal", principal_id, role_id)
        candidate["role_bindings"][binding_id] = {
            "id": binding_id,
            "subject_kind": "principal",
            "subject_id": principal_id,
            "role_id": role_id,
        }
        descriptor = identity.effective_descriptor(candidate, principal_id, canonical_ref)
        view_record = {
            "principal_id": principal_id,
            "descriptor_id": descriptor["id"],
            "descriptor_generation": 1,
            "canonical_ref": view["canonical_ref"],
            "projected_ref": view["projected_ref"],
            "canonical_frontier": view["canonical_frontier"],
            "projected_tip": view["projected_tip"],
            "anchors": {},
        }
        for projected, anchor in view["anchors"].items():
            migrated = dict(anchor)
            migrated["descriptor_id"] = descriptor["id"]
            migrated["descriptor_generation"] = 1
            view_record["anchors"][projected] = migrated
        candidate["views"][view_name] = view_record
        assignments[view_name] = (descriptor["id"], 1)

    def assignment_for(view_name: str | None) -> dict:
        descriptor_id, generation = assignments[view_name]
        return {"descriptor_id": descriptor_id, "descriptor_generation": generation}

    events_by_tx: dict[str, dict] = {}
    for event in candidate["outbox"].values():
        events_by_tx[event["transaction_id"]] = event

    for identifier, transaction in sorted(candidate["transactions"].items()):
        targets: dict[str, dict] = {}
        # Use the corresponding event's immutable target list when one
        # exists; pending views are emptied as fanout completes.
        event = events_by_tx.get(identifier)
        if event is not None:
            target_names = sorted(event["target_views"])
        else:
            target_names = sorted(transaction["pending_views"])
        for name in target_names:
            targets[name] = assignment_for(name)
        if transaction.get("kind") != "canonical_update":
            transaction["principal_id"] = transaction.pop("principal")
            descriptor_id, generation = assignments[transaction["view"]]
            transaction["descriptor_id"] = descriptor_id
            transaction["descriptor_generation"] = generation
        transaction["target_descriptors"] = targets

    for event in candidate["outbox"].values():
        targets = candidate["transactions"][event["transaction_id"]]["target_descriptors"]
        event["target_descriptors"] = copy.deepcopy(targets)
        event["target_views"] = sorted(targets)

    for job in candidate["projection_jobs"].values():
        job["descriptor_id"], job["descriptor_generation"] = assignments[job["view"]]

    for change in candidate["changes"].values():
        owner = change.pop("owner", None)
        if owner is not None:
            change["owner_principal_id"] = owner
        for revision in change["revisions"]:
            descriptor_id, generation = assignments[revision["view"]]
            revision["descriptor_id"] = descriptor_id
            revision["descriptor_generation"] = generation

    candidate["version"] = 2
    return candidate


def migrate_identities(root: Path) -> None:
    """Upgrade a healthy quiescent version-1 forge in one state replacement."""
    with locked_unversioned(root):
        state = load_state(root)
        version = state.get("version")
        if version == 2:
            return
        if version != 1:
            raise ForgeError(f"unsupported forge state version: {version!r}")
        refs = collect_layout(root, state)
        check_loaded(root, state, *refs)
        _require_quiescent(root, state)
        candidate = _build_candidate(state)
        check_loaded(root, candidate, *refs)
        save_state(root, candidate)
    check_invariants(root)
