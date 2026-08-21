"""Read-only global invariant checks for version-1 and version-2 Bastion forges."""

from __future__ import annotations

import copy
import fcntl
import json
import os
import re
from pathlib import Path
from typing import Any

import identity
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
    capability_allowed,
    changed_entries,
    normalize_prefix,
    parse_change_id,
    validate_view_name,
    visible,
)


OID = re.compile(r"^[0-9a-f]{40}$")
SOURCE_STATUSES = {"prepared", "canonical_committed", "source_unconfirmed", "fanout_pending", "published", "aborted"}
ADMIN_STATUSES = {"prepared", "fanout_pending", "published", "aborted"}
EVENT_STATUSES = {"blocked", "pending", "dispatched"}
JOB_STATUSES = {"pending", "running", "ready", "retry", "completed"}
VERSION_1_KEYS = {
    "version", "canonical_branch", "views", "transactions", "changes", "outbox",
    "projection_jobs", "next_transaction_sequence",
}
VERSION_2_KEYS = VERSION_1_KEYS | {
    "principals", "teams", "roles", "role_bindings", "view_descriptors",
}


def _generation(value: object, subject: str) -> None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < 1
    ):
        _fail("STATE_SCHEMA", subject, "descriptor generation must be a positive integer")


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


def _string_list(record: dict, field: str, subject: str) -> None:
    if not isinstance(record[field], list) or not all(isinstance(item, str) for item in record[field]):
        _fail("STATE_SCHEMA", subject, f"{field} must be a list of strings")


def _anchor_schema(anchor: dict, projected: str, version: int) -> None:
    extra = {"descriptor_id", "descriptor_generation"} if version >= 2 else set()
    _required(anchor, {"canonical", "kind"} | extra, projected)
    if not _oid(anchor["canonical"]) or anchor["kind"] not in {"submitted", "synthetic"}:
        _fail("STATE_SCHEMA", projected, "invalid anchor")
    if anchor["kind"] == "submitted":
        expected = {"canonical", "kind"} | extra
        if set(anchor) != expected:
            _fail("STATE_SCHEMA", projected, "submitted anchor has unexpected fields")
    else:
        _required(anchor, {"canonical", "kind", "change_id"} | extra, projected)
        allowed_fields = {"canonical", "kind", "change_id"} | extra
        if set(anchor) != allowed_fields:
            _fail("STATE_SCHEMA", projected, "synthetic anchor has unexpected fields")
        if anchor["change_id"] is not None and not isinstance(anchor["change_id"], str):
            _fail("STATE_SCHEMA", projected, "synthetic change_id must be string or null")
    if version >= 2:
        if not isinstance(anchor["descriptor_id"], str):
            _fail("STATE_SCHEMA", projected, "anchor descriptor assignment is invalid")
        if version == 2:
            if anchor["descriptor_generation"] != 1:
                _fail("STATE_SCHEMA", projected, "anchor descriptor generation must be 1")
        else:
            _generation(anchor["descriptor_generation"], projected)


def _assignment(record: dict, subject: str, code: str = "STATE_SCHEMA", version: int = 3) -> None:
    value = record.get("target_descriptors")
    if not isinstance(value, dict):
        _fail(code, subject, "target_descriptors must be a dictionary")
    for name, assignment in value.items():
        if not isinstance(name, str) or not isinstance(assignment, dict):
            _fail(code, subject, "target_descriptors must map names to assignments")
        if set(assignment) != {"descriptor_id", "descriptor_generation"}:
            _fail(code, subject, "target descriptor assignment has invalid shape")
        if not isinstance(assignment["descriptor_id"], str):
            _fail(code, subject, "target descriptor assignment is invalid")
        if version == 2:
            if assignment["descriptor_generation"] != 1:
                _fail(code, subject, "target descriptor generation must be 1")
        else:
            _generation(assignment["descriptor_generation"], subject)


def _transaction_schema(state: dict, version: int) -> None:
    sequences: set[int] = set()
    max_sequence = 0
    extra_source = (
        {"principal_id", "descriptor_id", "descriptor_generation"}
        if version >= 2
        else {"principal"}
    )
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
            if version >= 2:
                _assignment(transaction, identifier, version=version)
                if not set(transaction["pending_views"]) <= set(transaction["target_descriptors"]):
                    _fail("STATE_SCHEMA", identifier, "administrative pending views exceed captured targets")
            if version >= 3:
                _string(transaction, "principal_id", identifier)
        elif "kind" in transaction:
            _fail("STATE_SCHEMA", identifier, "unknown transaction kind")
        else:
            _required(transaction, {"view", "projected_ref", "canonical_frontier_old", "projected_old", "projected_new", "mappings"} | extra_source, identifier)
            if transaction["status"] not in SOURCE_STATUSES:
                _fail("STATE_SCHEMA", identifier, "invalid source status")
            for field in ("view", "projected_ref", "canonical_frontier_old", "projected_old", "projected_new"):
                _string(transaction, field, identifier)
                if field.endswith("old") or field.endswith("new"):
                    if not _oid(transaction[field]):
                        _fail("STATE_SCHEMA", identifier, f"{field} must be a nonzero full object ID")
            if version >= 2:
                _string(transaction, "principal_id", identifier)
                _string(transaction, "descriptor_id", identifier)
                if version == 2:
                    if transaction["descriptor_generation"] != 1:
                        _fail("STATE_SCHEMA", identifier, "source descriptor generation must be 1")
                else:
                    _generation(transaction["descriptor_generation"], identifier)
                _assignment(transaction, identifier, version=version)
                if set(transaction["target_descriptors"]) < set(transaction["pending_views"]):
                    _fail("STATE_SCHEMA", identifier, "pending views exceed captured targets")
            else:
                _string(transaction, "principal", identifier)
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


def _schema_v1(state: dict) -> None:
    if set(state) != VERSION_1_KEYS:
        _fail("STATE_SCHEMA", "state", "top-level keys do not match version 1")
    if state["version"] != 1 or isinstance(state["version"], bool):
        _fail("STATE_SCHEMA", "state", "version must be integer 1")
    _common_map_skeleton(state, VERSION_1_KEYS - {"version"})
    _schema_views(state, 1)
    _transaction_schema(state, 1)
    _outbox_job_schema(state, 1)


def _schema_v2(state: dict) -> None:
    if set(state) != VERSION_2_KEYS:
        _fail("STATE_SCHEMA", "state", "top-level keys do not match version 2")
    if state["version"] != 2 or isinstance(state["version"], bool):
        _fail("STATE_SCHEMA", "state", "version must be integer 2")
    catalog_keys = VERSION_2_KEYS - VERSION_1_KEYS - {"version"}
    _common_map_skeleton(state, VERSION_2_KEYS - {"version"})
    for name in sorted(catalog_keys):
        for key, value in state[name].items():
            if not isinstance(key, str) or not isinstance(value, dict):
                _fail("STATE_SCHEMA", name, "catalog identifiers and records must be strings and dictionaries")
    _schema_catalog(state, 2)
    _schema_views(state, 2)
    _transaction_schema(state, 2)
    _outbox_job_schema(state, 2)


def _schema_v3(state: dict) -> None:
    if set(state) != VERSION_2_KEYS:
        _fail("STATE_SCHEMA", "state", "top-level keys do not match version 3")
    if state["version"] != 3 or isinstance(state["version"], bool):
        _fail("STATE_SCHEMA", "state", "version must be integer 3")
    catalog_keys = VERSION_2_KEYS - VERSION_1_KEYS - {"version"}
    _common_map_skeleton(state, VERSION_2_KEYS - {"version"})
    for name in sorted(catalog_keys):
        for key, value in state[name].items():
            if not isinstance(key, str) or not isinstance(value, dict):
                _fail("STATE_SCHEMA", name, "catalog identifiers and records must be strings and dictionaries")
    _schema_catalog(state, 3)
    _schema_views(state, 3)
    _transaction_schema(state, 3)
    _outbox_job_schema(state, 3)


def _capability_map_schema(role: dict, field: str, role_id: str) -> None:
    value = role[field]
    if not isinstance(value, dict) or set(value) != set(identity.CAPABILITIES):
        _fail("IDENTITY", role_id, f"{field} must hold exactly the nine capabilities")
    for capability in identity.CAPABILITIES:
        grants = value[capability]
        if not isinstance(grants, list) or not all(isinstance(item, str) for item in grants):
            _fail("IDENTITY", role_id, f"{field}.{capability} must be a list of strings")
        try:
            normalized = [normalize_prefix(item) for item in grants]
        except ForgeError:
            _fail("IDENTITY", role_id, f"invalid {field}.{capability} prefix")
        if grants != normalized or grants != sorted(set(grants)):
            _fail("IDENTITY", role_id, f"{field}.{capability} grants are not normalized, sorted, and unique")
        if identity.minimize_prefixes(grants) != grants:
            _fail("IDENTITY", role_id, f"{field}.{capability} grants are not minimized")


def _schema_catalog(state: dict, version: int) -> None:
    for identifier, principal in sorted(state["principals"].items()):
        try:
            identity.validate_identifier(identifier)
        except ForgeError:
            _fail("IDENTITY", identifier, "invalid principal identifier")
        if principal["id"] != identifier:
            _fail("IDENTITY", identifier, "principal key does not match id")
        kind = principal.get("kind")
        if kind == "user":
            if set(principal) != {"id", "kind"}:
                _fail("IDENTITY", identifier, "user record has unexpected fields")
        elif kind == "service_account":
            if set(principal) != {"id", "kind", "owner_principal_id"}:
                _fail("IDENTITY", identifier, "service account has unexpected fields")
            owner = principal["owner_principal_id"]
            if not isinstance(owner, str) or state["principals"].get(owner, {}).get("kind") != "user":
                _fail("IDENTITY", identifier, "service-account owner must be an existing user")
        else:
            _fail("IDENTITY", identifier, "principal kind is invalid")
    for identifier, team in sorted(state["teams"].items()):
        try:
            identity.validate_identifier(identifier)
        except ForgeError:
            _fail("IDENTITY", identifier, "invalid team identifier")
        if team["id"] != identifier or set(team) != {"id", "members"}:
            _fail("IDENTITY", identifier, "team record does not match its id or shape")
        members = team["members"]
        if not isinstance(members, list) or members != sorted(set(members)) or not all(isinstance(member, str) for member in members):
            _fail("IDENTITY", identifier, "team members must be sorted and unique")
        if any(member not in state["principals"] for member in members):
            _fail("IDENTITY", identifier, "team member is not an existing principal")
    for role_id, role in sorted(state["roles"].items()):
        try:
            identity.validate_identifier(role_id)
        except ForgeError:
            _fail("IDENTITY", role_id, "invalid role identifier")
        if version == 2:
            if role["id"] != role_id or set(role) != {"id", "read", "write"}:
                _fail("IDENTITY", role_id, "role record does not match its id or shape")
            for field in ("read", "write"):
                _string_list(role, field, role_id)
                try:
                    normalized = [normalize_prefix(value) for value in role[field]]
                except ForgeError:
                    _fail("IDENTITY", role_id, f"invalid {field} prefix")
                if role[field] != normalized or role[field] != sorted(set(role[field])):
                    _fail("IDENTITY", role_id, f"{field} grants are not normalized, sorted, and unique")
                if identity.minimize_prefixes(role[field]) != role[field]:
                    _fail("IDENTITY", role_id, f"{field} grants are not minimized")
            if not role["read"] and not role["write"]:
                _fail("IDENTITY", role_id, "role has no grants")
        else:
            if role["id"] != role_id or set(role) != {"id", "allow", "deny"}:
                _fail("IDENTITY", role_id, "role record does not match its id or shape")
            _capability_map_schema(role, "allow", role_id)
            _capability_map_schema(role, "deny", role_id)
            if not any(role["allow"][capability] for capability in identity.CAPABILITIES):
                _fail("IDENTITY", role_id, "role has no allow grants")
    for identifier, binding in sorted(state["role_bindings"].items()):
        if binding["id"] != identifier or set(binding) != {"id", "subject_kind", "subject_id", "role_id"}:
            _fail("IDENTITY", identifier, "binding record does not match its id or shape")
        if binding["subject_kind"] not in {"principal", "team"}:
            _fail("IDENTITY", identifier, "binding subject kind is invalid")
        expected = identity.binding_identifier(binding["subject_kind"], binding["subject_id"], binding["role_id"])
        if identifier != expected:
            _fail("IDENTITY", identifier, "binding identifier is not deterministic")
        subject_map = "principals" if binding["subject_kind"] == "principal" else "teams"
        if binding["subject_id"] not in state[subject_map]:
            _fail("IDENTITY", identifier, "binding subject does not exist")
        if binding["role_id"] not in state["roles"]:
            _fail("IDENTITY", identifier, "binding role does not exist")


def _schema_views(state: dict, version: int) -> None:
    if git(Path("."), "check-ref-format", "--branch", state["canonical_branch"], check=False).returncode:
        _fail("STATE_SCHEMA", "state", "canonical_branch is not a valid branch")
    for name, view in sorted(state["views"].items()):
        try:
            validate_view_name(name)
        except ForgeError:
            _fail("STATE_SCHEMA", name, "invalid view name")
        base = {"canonical_ref", "projected_ref", "canonical_frontier", "projected_tip", "anchors"}
        policy = {"principal", "read", "write"} if version == 1 else {"principal_id", "descriptor_id", "descriptor_generation"}
        _required(view, base | policy, name)
        if version >= 2:
            unexpected = set(view) - (base | policy)
            if unexpected:
                _fail("STATE_SCHEMA", name, f"unexpected view fields {sorted(unexpected)[0]}")
        for field in ("canonical_ref", "projected_ref", "canonical_frontier", "projected_tip"):
            _string(view, field, name)
        if version == 1:
            _string(view, "principal", name)
            for field in ("read", "write"):
                _string_list(view, field, name)
        else:
            _string(view, "principal_id", name)
            _string(view, "descriptor_id", name)
            if version == 2:
                if view["descriptor_generation"] != 1:
                    _fail("STATE_SCHEMA", name, "descriptor generation must be integer 1")
            else:
                _generation(view["descriptor_generation"], name)
        if not isinstance(view["anchors"], dict):
            _fail("STATE_SCHEMA", name, "anchors must be a dictionary")
        for projected, anchor in view["anchors"].items():
            if not isinstance(projected, str) or not isinstance(anchor, dict):
                _fail("STATE_SCHEMA", name, "anchors must map strings to dictionaries")
            if not _oid(projected):
                _fail("STATE_SCHEMA", name, "anchor key is not a full object ID")
            _anchor_schema(anchor, projected, version)


def _outbox_job_schema(state: dict, version: int) -> None:
    for identifier, event in sorted(state["outbox"].items()):
        _required(event, {"id", "type", "transaction_id", "sequence", "canonical_to", "target_views", "status"}, identifier)
        if event["id"] != identifier or not isinstance(event["id"], str): _fail("STATE_SCHEMA", identifier, "event key does not match id")
        if not isinstance(event["transaction_id"], str) or not _is_int(event["sequence"], 1) or not _oid(event["canonical_to"]) or not isinstance(event["target_views"], list) or not all(isinstance(value, str) for value in event["target_views"]) or event["status"] not in EVENT_STATUSES or not isinstance(event["type"], str):
            _fail("STATE_SCHEMA", identifier, "event has invalid field types")
        if version >= 2:
            _assignment(event, identifier, version=version)
            if event["target_views"] != sorted(event["target_descriptors"]):
                _fail("STATE_SCHEMA", identifier, "event target views do not match captured targets")
    for identifier, job in sorted(state["projection_jobs"].items()):
        _required(job, {"id", "event_id", "transaction_id", "view", "sequence", "canonical_to", "status", "attempts", "last_error", "next_attempt_at"}, identifier)
        if job["id"] != identifier or not isinstance(job["id"], str): _fail("STATE_SCHEMA", identifier, "job key does not match id")
        if not all(isinstance(job[field], str) for field in ("event_id", "transaction_id", "view", "status")) or not _is_int(job["sequence"], 1) or not _oid(job["canonical_to"]) or job["status"] not in JOB_STATUSES or not _is_int(job["attempts"], 0) or (job["last_error"] is not None and not isinstance(job["last_error"], str)) or not isinstance(job["next_attempt_at"], (int, float)):
            _fail("STATE_SCHEMA", identifier, "job has invalid field types")
        if version >= 2:
            _string(job, "descriptor_id", identifier)
            if version == 2:
                if job["descriptor_generation"] != 1:
                    _fail("STATE_SCHEMA", identifier, "job descriptor generation must be 1")
            else:
                _generation(job["descriptor_generation"], identifier)
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
        if version >= 2:
            for revision in change["revisions"]:
                if not isinstance(revision, dict):
                    _fail("STATE_SCHEMA", str(change_id), "revision is not a dictionary")
                _string(revision, "descriptor_id", str(change_id))
                if version == 2:
                    if revision.get("descriptor_generation") != 1:
                        _fail("STATE_SCHEMA", str(change_id), "revision descriptor generation must be 1")
                else:
                    _generation(revision.get("descriptor_generation"), str(change_id))
        owned = "owner_principal_id" if version >= 2 else "owner"
        for field in (owned, "canonical_ref"):
            if field in change and not isinstance(change[field], str):
                _fail("STATE_SCHEMA", str(change_id), f"{field} must be a string")


_MAP_FIELDS = ("principals", "teams", "roles", "role_bindings", "view_descriptors", "views", "transactions", "changes", "outbox", "projection_jobs")


def _common_map_skeleton(state: dict, names: set[str]) -> None:
    if not isinstance(state["canonical_branch"], str) or not state["canonical_branch"]:
        _fail("STATE_SCHEMA", "state", "canonical_branch must be a non-empty string")
    if not _is_int(state["next_transaction_sequence"], 1):
        _fail("STATE_SCHEMA", "state", "next_transaction_sequence must be a positive integer")
    for name in sorted(names & set(_MAP_FIELDS)):
        if not isinstance(state[name], dict):
            _fail("STATE_SCHEMA", "state", f"{name} must be a dictionary")
        if name in {"views", "transactions", "changes", "outbox", "projection_jobs"}:
            for key, value in state[name].items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    _fail("STATE_SCHEMA", name, "record identifiers and records must be strings and dictionaries")


_LEGACY_DESCRIPTOR_KEYS = {
    "algorithm", "principal_id", "principal_kind", "canonical_ref",
    "team_ids", "role_ids", "binding_ids", "read", "write",
}
_CAPABILITY_DESCRIPTOR_KEYS = {
    "algorithm", "principal_id", "principal_kind", "canonical_ref",
    "team_ids", "role_ids", "binding_ids", "capabilities", "restrictions",
}


def _provenance_schema(payload: dict, identifier: str) -> None:
    for field in ("team_ids", "role_ids", "binding_ids"):
        _string_list(payload, field, identifier)
        if payload[field] != sorted(set(payload[field])):
            _fail("DESCRIPTOR", identifier, f"{field} is not sorted and unique")
    if not isinstance(payload["principal_kind"], str) or not isinstance(payload["canonical_ref"], str):
        _fail("DESCRIPTOR", identifier, "descriptor provenance has invalid types")


def _validate_descriptor_shape(payload: dict, identifier: str) -> str:
    algorithm = payload.get("algorithm")
    if algorithm == identity.DESCRIPTOR_ALGORITHM:
        if set(payload) != _CAPABILITY_DESCRIPTOR_KEYS:
            _fail("DESCRIPTOR", identifier, "descriptor has invalid shape")
        for field in ("capabilities", "restrictions"):
            value = payload[field]
            if not isinstance(value, dict) or set(value) != set(identity.CAPABILITIES):
                _fail("DESCRIPTOR", identifier, f"descriptor {field} must hold nine capabilities")
            for capability in identity.CAPABILITIES:
                areas = value[capability]
                if not isinstance(areas, list) or not all(isinstance(item, str) for item in areas):
                    _fail("DESCRIPTOR", identifier, f"descriptor {field}.{capability} must be a list of strings")
                if areas != identity.minimize_prefixes(areas):
                    _fail("DESCRIPTOR", identifier, f"descriptor {field}.{capability} is not minimized")
    elif algorithm == identity.LEGACY_DESCRIPTOR_ALGORITHM:
        if set(payload) != _LEGACY_DESCRIPTOR_KEYS:
            _fail("DESCRIPTOR", identifier, "legacy descriptor has invalid shape")
        for field in ("read", "write"):
            _string_list(payload, field, identifier)
            if payload[field] != identity.minimize_prefixes(payload[field]):
                _fail("DESCRIPTOR", identifier, f"legacy descriptor {field} is not minimized")
    else:
        _fail("DESCRIPTOR", identifier, "unknown descriptor algorithm")
    _provenance_schema(payload, identifier)
    return algorithm


def _recompute_descriptor(state: dict, identifier: str, payload: dict) -> None:
    principal = state["principals"].get(payload["principal_id"])
    if principal is None or principal["kind"] != payload["principal_kind"]:
        _fail("DESCRIPTOR", identifier, "descriptor principal does not match catalog")
    if payload["canonical_ref"] != f"refs/heads/{state['canonical_branch']}":
        _fail("DESCRIPTOR", identifier, "descriptor canonical ref is not configured")
    # Catalog additions are dormant until a policy transition, so the
    # descriptor is recomputed from its recorded provenance over the
    # current (append-only) catalog rather than from live selection.
    for role_id in payload["role_ids"]:
        if role_id not in state["roles"]:
            _fail("DESCRIPTOR", identifier, "descriptor role does not exist")
    try:
        allow, deny = identity.effective_grants(state, payload["role_ids"])
    except ForgeError as error:
        _fail("DESCRIPTOR", identifier, f"cannot resolve descriptor roles: {error}")
    if [allow, deny] != [payload["capabilities"], payload["restrictions"]]:
        _fail("DESCRIPTOR", identifier, "descriptor effective grants differ from resolution")
    computed = identity.compute_descriptor_id(payload)
    if computed != identifier:
        _fail("DESCRIPTOR", identifier, "descriptor identity does not match canonical serialization")


def _recompute_legacy_descriptor(state: dict, identifier: str, payload: dict) -> None:
    principal = state["principals"].get(payload["principal_id"])
    if principal is None or principal["kind"] != payload["principal_kind"]:
        _fail("DESCRIPTOR", identifier, "descriptor principal does not match catalog")
    if payload["canonical_ref"] != f"refs/heads/{state['canonical_branch']}":
        _fail("DESCRIPTOR", identifier, "descriptor canonical ref is not configured")
    try:
        team_ids, binding_ids, role_ids = identity.resolve_principal(
            state, payload["principal_id"]
        )
        read_union: list[str] = []
        write_union: list[str] = []
        for role_id in role_ids:
            role = state["roles"][role_id]
            read_union.extend(role["read"])
            write_union.extend(role["write"])
        effective_read = identity.minimize_prefixes(read_union)
        effective_write = identity.minimize_prefixes(
            identity.intersect_prefixes(effective_read, write_union)
        )
    except ForgeError as error:
        _fail("DESCRIPTOR", identifier, f"cannot resolve principal: {error}")
    if (team_ids, binding_ids, role_ids) != (
        payload["team_ids"], payload["binding_ids"], payload["role_ids"]
    ):
        _fail("DESCRIPTOR", identifier, "descriptor provenance differs from fresh resolution")
    if [effective_read, effective_write] != [payload["read"], payload["write"]]:
        _fail("DESCRIPTOR", identifier, "descriptor effective grants differ from resolution")
    computed = identity.compute_descriptor_id(payload)
    if computed != identifier:
        _fail("DESCRIPTOR", identifier, "descriptor identity does not match canonical serialization")


def _identity_resolution_checks(state: dict) -> None:
    """Version 2 recomputes every descriptor; version 3 validates integrity.

    Version-3 current assignments recompute from their recorded provenance
    (roles are immutable), which detects state tampering while permitting
    dormant catalog additions; all other descriptors are checked
    structurally and by canonical hash only.
    """
    version = state["version"]
    assigned: set[str] = {view["descriptor_id"] for view in state["views"].values()}
    for identifier, descriptor in sorted(state["view_descriptors"].items()):
        payload = {
            key: value for key, value in descriptor.items() if key != "id"
        }
        algorithm = _validate_descriptor_shape(payload, identifier)
        if version == 2:
            if algorithm == identity.LEGACY_DESCRIPTOR_ALGORITHM:
                _recompute_legacy_descriptor(state, identifier, payload)
            else:
                _recompute_descriptor(state, identifier, payload)
            continue
        if identifier in assigned and algorithm == identity.DESCRIPTOR_ALGORITHM:
            _recompute_descriptor(state, identifier, payload)
        elif identity.compute_descriptor_id(payload) != identifier:
            _fail("DESCRIPTOR", identifier, "descriptor identity does not match canonical serialization")
    owners: dict[str, str] = {}
    for name, view in sorted(state["views"].items()):
        principal_id = view["principal_id"]
        if principal_id not in state["principals"]:
            _fail("DESCRIPTOR", name, "view principal does not exist")
        descriptor = state["view_descriptors"].get(view["descriptor_id"])
        if descriptor is None:
            _fail("DESCRIPTOR", name, "view references an unknown descriptor")
        if descriptor["principal_id"] != principal_id or descriptor["canonical_ref"] != view["canonical_ref"]:
            _fail("DESCRIPTOR", name, "view and descriptor disagree")
        previous = owners.setdefault(principal_id, view["descriptor_id"])
        if previous != view["descriptor_id"]:
            _fail("DESCRIPTOR", principal_id, "one principal has views bound to different descriptors")


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


def _descriptor_policy(descriptor: dict) -> dict:
    """Adapt a legacy captured descriptor to capability-map form."""
    if "capabilities" in descriptor:
        return descriptor
    read = descriptor["read"]
    effect_areas = identity.minimize_prefixes(
        identity.intersect_prefixes(read, descriptor["write"])
    )
    allow = {capability: [] for capability in identity.CAPABILITIES}
    allow["read"] = read
    for capability in ("create", "modify", "delete", "mode", "symlink"):
        allow[capability] = effect_areas
    return {
        "capabilities": allow,
        "restrictions": {capability: [] for capability in identity.CAPABILITIES},
    }


def _filter_by_policy(entries: dict, policy: object) -> dict:
    """Filter flat leaves by a read-prefix list or either descriptor shape."""
    if isinstance(policy, list):
        return _filtered(entries, policy)
    if "capabilities" in policy:
        return {path: entry for path, entry in entries.items() if visible(path, policy)}
    return _filtered(entries, policy["read"])


def _view_policy(state: dict, name: str, version: int) -> object:
    if version == 1:
        return state["views"][name]["read"]
    descriptor = state["view_descriptors"][state["views"][name]["descriptor_id"]]
    if state["version"] >= 3:
        return descriptor
    return descriptor["read"]


def _policy_frontiers(root: Path, state: dict, canonical_history: list[str], tip: str, version: int) -> None:
    index = {oid: position for position, oid in enumerate(canonical_history)}
    for name, view in sorted(state["views"].items()):
        if version == 1:
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


def _source_transactions(root: Path, state: dict, canonical_history: list[str], tip: str, version: int) -> None:
    canonical = canonical_repo(root); index = {oid: number for number, oid in enumerate(canonical_history)}
    for identifier, tx in sorted(state["transactions"].items(), key=lambda item: (item[1]["sequence"], item[0])):
        if tx.get("kind") == "canonical_update": continue
        view = state["views"].get(tx["view"])
        if version == 1:
            if not view or tx["principal"] != tx["view"] or tx["principal"] != view["principal"]: _fail("MAPPING", identifier, "source view or principal is invalid")
            policy = view["read"]
            legacy_read, legacy_write = view["read"], view["write"]
            descriptor = None
        else:
            if not view: _fail("MAPPING", identifier, "source view is invalid")
            if tx["principal_id"] != view["principal_id"]: _fail("MAPPING", identifier, "source principal differs from view")
            descriptor = state["view_descriptors"].get(tx["descriptor_id"])
            if descriptor is None: _fail("DESCRIPTOR", identifier, "source descriptor is unknown")
            if version == 2 and (tx["descriptor_id"] != view["descriptor_id"] or tx["descriptor_generation"] != view["descriptor_generation"]):
                _fail("DESCRIPTOR", identifier, "source descriptor differs from view assignment")
            # Source transactions authorize with their captured descriptor
            # regardless of era; the current assignment may have moved on.
            policy = _descriptor_policy(descriptor)
            if version == 2:
                legacy_read, legacy_write = descriptor["read"], descriptor["write"]
        if tx["canonical_ref"] != view["canonical_ref"] or tx["projected_ref"] != view["projected_ref"] or tx["id"] != transaction_id(tx["view"], tx["projected_old"], tx["projected_new"]): _fail("MAPPING", identifier, "source transaction identity or refs differ")
        if not tx["mappings"]: _fail("MAPPING", identifier, "source transaction has no mappings")
        if len(tx["pending_views"]) != len(set(tx["pending_views"])) or tx["view"] in tx["pending_views"] or any(value not in state["views"] for value in tx["pending_views"]): _fail("MAPPING", identifier, "invalid pending views")
        if tx["canonical_frontier_old"] not in index or tx["canonical_old"] not in index or index[tx["canonical_frontier_old"]] > index[tx["canonical_old"]]: _fail("MAPPING", identifier, "invalid canonical frontier range")
        for oid in canonical_history[index[tx["canonical_frontier_old"]]:index[tx["canonical_old"]] + 1]:
            if _filter_by_policy(_flat(canonical, oid, "MAPPING"), policy) != _flat(view_repo(root, tx["view"]), tx["projected_old"], "MAPPING"): _fail("MAPPING", identifier, "canonical prehistory was not hidden-only")
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
            try:
                if version >= 3:
                    authorize_changes(changes, submitted_parent_entries, _descriptor_policy(descriptor))
                else:
                    for path in changes:
                        if not allowed(path, legacy_write) or not allowed(path, legacy_read):
                            raise ForgeError("unauthorized")
                applied = apply_changes(_flat(canonical, canonical_parent, "MAPPING"), changes)
            except ForgeError: _fail("MAPPING", identifier, "mapping contains unauthorized or invalid changes")
            if applied != _flat(canonical, canon, "MAPPING") or _filter_by_policy(_flat(canonical, canon, "MAPPING"), policy) != submitted_entries: _fail("MAPPING", identifier, "mapping tree does not reconcile exactly")
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
        if version == 2 and tx["status"] in {"fanout_pending", "published"}:
            for mapping in tx["mappings"]:
                anchor = view["anchors"].get(mapping["submitted"])
                if not anchor or anchor.get("descriptor_id") != tx["descriptor_id"] or anchor.get("descriptor_generation") != tx["descriptor_generation"]:
                    _fail("DESCRIPTOR", identifier, "confirmed anchor lacks the source descriptor assignment")


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
            if state.get("version") and state["version"] >= 2:
                if not set(tx["pending_views"]) <= set(tx.get("target_descriptors", {})): _fail("DESCRIPTOR", identifier, "administrative pending views exceed captured targets")
                if state["version"] == 2 or tx["status"] in {"prepared", "fanout_pending"}:
                    for name, assignment in tx["target_descriptors"].items():
                        view_assignment = state["views"][name]
                        if assignment != {"descriptor_id": view_assignment["descriptor_id"], "descriptor_generation": view_assignment["descriptor_generation"]}:
                            _fail("DESCRIPTOR", identifier, "administrative target assignment differs from the view")
            if state.get("version") >= 3:
                principal_id = tx.get("principal_id")
                if not isinstance(principal_id, str) or principal_id not in state["principals"]:
                    _fail("ADMIN", identifier, "administrative transaction lacks an existing acting principal")
                try:
                    _team_ids, _binding_ids, role_ids = identity.resolve_principal(state, principal_id)
                    allow, restrictions = identity.effective_grants(state, role_ids)
                except ForgeError as error:
                    _fail("ADMIN", identifier, f"cannot resolve acting principal: {error}")
                before = _flat(canonical, tx["canonical_old"], "CANONICAL_REF")
                after = _flat(canonical, tx["canonical_new"], "CANONICAL_REF")
                for path in sorted(before.keys() | after.keys()):
                    if before.get(path) == after.get(path):
                        continue
                    if not capability_allowed(allow["admin"], restrictions["admin"], path):
                        _fail("ADMIN", identifier, "acting principal lacks admin coverage of a changed path")
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


def _anchor_assignment_matches(anchor: dict, descriptor_id: str, generation: int, code: str, subject: str) -> None:
    if anchor.get("descriptor_id") != descriptor_id or anchor.get("descriptor_generation") != generation:
        _fail(code, subject, "anchor descriptor assignment differs from the view")


def _projected(root: Path, state: dict, canonical_history: list[str], version: int) -> tuple[dict[str, list[tuple]], dict[str, tuple[set[str], set[str], set[str]]]]:
    canonical = canonical_repo(root); index = {oid: i for i, oid in enumerate(canonical_history)}; revision_data: dict[str, list[tuple]] = {}
    confirmed: dict[str, dict[str, tuple[str, str | None]]] = {name: {} for name in state["views"]}
    view_closures: dict[str, tuple[set[str], set[str], set[str]]] = {}
    for tx in state["transactions"].values():
        if tx.get("kind") is None and tx["status"] in {"fanout_pending", "published"}:
            for mapping in tx["mappings"]:
                if mapping["canonical"] in confirmed[tx["view"]]: _fail("ANCHOR", tx["view"], "multiple confirmed mappings for canonical commit")
                confirmed[tx["view"]][mapping["canonical"]] = (mapping["submitted"], mapping["change_id"])
    for name, view in sorted(state["views"].items()):
        repo = view_repo(root, name); actual = _history(repo, view["projected_tip"], "PROJECTION", name)
        if not actual: _fail("PROJECTION", name, "projected history is empty")
        allowed_commits: set[str] = set(actual)
        allowed_trees: set[str] = set()
        allowed_blobs: set[str] = set()
        if version <= 2:
            read_policy = _view_policy(state, name, version)
            descriptor_id = view.get("descriptor_id") if version == 2 else None
            generation = view.get("descriptor_generation") if version == 2 else None
            expected: list[tuple[str, str, str | None]] = []; seen: dict | None = None; actual_index = 0; previous_projected: str | None = None
            for canonical_oid in canonical_history[:index[view["canonical_frontier"]] + 1]:
                filtered = _filtered(_flat(canonical, canonical_oid, "PROJECTION"), read_policy)
                mapping = confirmed[name].get(canonical_oid)
                if mapping: projected, kind, change_id = mapping[0], "submitted", mapping[1]
                elif filtered != seen:
                    if actual_index >= len(actual): _fail("PROJECTION", name, "missing projected representation")
                    projected, kind, change_id = actual[actual_index], "synthetic", parse_change_id(_commit_parts(_raw_commit(canonical, canonical_oid, "PROJECTION", canonical_oid), "PROJECTION", canonical_oid)[3])
                else:
                    continue
                expected.append((projected, canonical_oid, kind)); actual_index += 1
                anchor = view["anchors"].get(projected)
                if not anchor or anchor["canonical"] != canonical_oid or anchor["kind"] != kind: _fail("ANCHOR", projected, "anchor does not match canonical replay")
                if version == 2: _anchor_assignment_matches(anchor, descriptor_id, generation, "DESCRIPTOR", projected)
                raw = _raw_commit(repo, projected, "PROJECTION", projected)
                _tree, parents, _lines, _message = _commit_parts(raw, "PROJECTION", projected)
                if parents != ([] if previous_projected is None else [previous_projected]): _fail("PROJECTION", projected, "projected history parent differs")
                if _flat(repo, projected, "PROJECTION") != filtered: _fail("PROJECTION", projected, "projected tree differs from canonical filter")
                allowed_trees.add(_tree)
                allowed_trees.update(_no_empty(repo, projected, "PROJECTION"))
                allowed_blobs.update(blob_oid for _mode, _kind, blob_oid in filtered.values())
                if kind == "synthetic":
                    if set(anchor) != {"canonical", "kind", "change_id"} | ({"descriptor_id", "descriptor_generation"} if version == 2 else set()) or anchor["change_id"] != change_id: _fail("ANCHOR", projected, "synthetic anchor has invalid Change-Id")
                    _synthetic(raw, projected, canonical_oid, previous_projected, change_id, "PROJECTION")
                    if change_id: revision_data.setdefault(change_id, []).append((name, projected, canonical_oid, "synthetic", descriptor_id, generation))
                else:
                    if set(anchor) != {"canonical", "kind"} | ({"descriptor_id", "descriptor_generation"} if version == 2 else set()): _fail("ANCHOR", projected, "submitted anchor has invalid shape")
                previous_projected = projected; seen = filtered
            if [item[0] for item in expected] != actual or set(view["anchors"]) != set(actual): _fail("ANCHOR", name, "anchors and projected history differ")
        else:
            _replay_eras(
                root,
                state,
                canonical,
                canonical_history,
                index,
                name,
                view,
                actual,
                confirmed[name],
                repo,
                allowed_trees,
                allowed_blobs,
                revision_data,
            )
        current_policy = _view_policy(state, name, version)
        if _flat(repo, view["projected_tip"], "PROJECTION") != _filter_by_policy(_flat(canonical, view["canonical_frontier"], "PROJECTION"), current_policy): _fail("PROJECTION", name, "tip tree differs from frontier filter")
        view_closures[name] = (allowed_commits, allowed_trees, allowed_blobs)
    return revision_data, view_closures


def _segment_descriptor(state: dict, anchor: dict, subject: str) -> dict:
    descriptor = state["view_descriptors"].get(anchor.get("descriptor_id"))
    if descriptor is None:
        _fail("ANCHOR", subject, "anchor references an unknown descriptor")
    return descriptor


def _replay_eras(
    root: Path,
    state: dict,
    canonical: Path,
    canonical_history: list[str],
    index: dict[str, int],
    name: str,
    view: dict,
    actual: list[str],
    confirmed_map: dict[str, tuple[str, str | None]],
    repo: Path,
    allowed_trees: set[str],
    allowed_blobs: set[str],
    revision_data: dict[str, list[tuple]],
) -> None:
    """Era-based replay: each anchor carries the descriptor that produced it."""
    segments: list[list[str]] = []
    for projected in actual:
        anchor = view["anchors"].get(projected)
        if anchor is None:
            _fail("ANCHOR", projected, "projected commit lacks an anchor")
        if (
            segments
            and segments[-1][0] == anchor["descriptor_id"]
            and segments[-1][1] == anchor["descriptor_generation"]
        ):
            segments[-1][2].append(projected)
        else:
            segments.append([anchor["descriptor_id"], anchor["descriptor_generation"], [projected]])
    frontier_index = index[view["canonical_frontier"]]
    cursor = 0
    previous_flat: dict | None = None
    previous_projected: str | None = None
    previous_descriptor: dict | None = None
    previous_generation: int | None = None
    expected: list[str] = []
    for order, (descriptor_id, generation, projected_ids) in enumerate(segments):
        descriptor = _segment_descriptor(state, view["anchors"][projected_ids[0]], projected_ids[0])
        if descriptor["principal_id"] != view["principal_id"]:
            _fail("DESCRIPTOR", name, "era descriptor principal differs from the view")
        first_anchor = view["anchors"][projected_ids[0]]
        if first_anchor["canonical"] not in index:
            _fail("ANCHOR", projected_ids[0], "era anchor is not on canonical history")
        start = index[first_anchor["canonical"]]
        last_anchor = view["anchors"][projected_ids[-1]]
        if last_anchor["canonical"] not in index:
            _fail("ANCHOR", projected_ids[-1], "era anchor is not on canonical history")
        end = index[last_anchor["canonical"]]
        if order == 0:
            if start != 0:
                _fail("ANCHOR", name, "first era does not begin at the canonical root")
        else:
            assert previous_descriptor is not None and previous_generation is not None
            for position in range(cursor, start):
                if _filter_by_policy(_flat(canonical, canonical_history[position], "PROJECTION"), previous_descriptor) != previous_flat:
                    _fail("PROJECTION", name, "inter-era canonical commits were not hidden-only")
            if generation <= previous_generation:
                _fail("DESCRIPTOR", name, "era generations are not monotone")
            if descriptor["principal_id"] != previous_descriptor["principal_id"]:
                _fail("DESCRIPTOR", name, "era principals differ")
            if first_anchor["kind"] != "synthetic" or first_anchor["change_id"] is not None:
                _fail("ANCHOR", projected_ids[0], "era advance requires a policy-adjustment anchor")
            if start < index[view["anchors"][segments[order - 1][2][-1]]["canonical"]]:
                _fail("ANCHOR", projected_ids[0], "era begins before the previous era ended")
        seen: dict | None = previous_flat
        segment_index = 0
        for position in range(start, end + 1):
            canonical_oid = canonical_history[position]
            filtered = _filter_by_policy(_flat(canonical, canonical_oid, "PROJECTION"), descriptor)
            mapping = confirmed_map.get(canonical_oid)
            if mapping:
                projected, kind, change_id = mapping[0], "submitted", mapping[1]
            elif filtered != seen:
                if segment_index >= len(projected_ids):
                    _fail("PROJECTION", name, "missing projected representation")
                    raise AssertionError
                projected = projected_ids[segment_index]
                kind = "synthetic"
                change_id = parse_change_id(
                    _commit_parts(
                        _raw_commit(canonical, canonical_oid, "PROJECTION", canonical_oid),
                        "PROJECTION",
                        canonical_oid,
                    )[3]
                )
            else:
                continue
            segment_index += 1
            anchor = view["anchors"][projected]
            if anchor["canonical"] != canonical_oid or anchor["kind"] != kind:
                _fail("ANCHOR", projected, "anchor does not match canonical replay")
            raw = _raw_commit(repo, projected, "PROJECTION", projected)
            _tree, parents, _lines, _message = _commit_parts(raw, "PROJECTION", projected)
            if parents != ([] if previous_projected is None else [previous_projected]):
                _fail("PROJECTION", projected, "projected history parent differs")
            if _flat(repo, projected, "PROJECTION") != filtered:
                _fail("PROJECTION", projected, "projected tree differs from canonical filter")
            allowed_trees.add(_tree)
            allowed_trees.update(_no_empty(repo, projected, "PROJECTION"))
            allowed_blobs.update(blob_oid for _mode, _kind, blob_oid in filtered.values())
            if kind == "synthetic":
                if set(anchor) != {"canonical", "kind", "change_id", "descriptor_id", "descriptor_generation"} or anchor["change_id"] != change_id:
                    _fail("ANCHOR", projected, "synthetic anchor has invalid Change-Id")
                _synthetic(raw, projected, canonical_oid, previous_projected, change_id, "PROJECTION")
                if change_id:
                    revision_data.setdefault(change_id, []).append((name, projected, canonical_oid, "synthetic", descriptor_id, generation))
            else:
                if set(anchor) != {"canonical", "kind", "descriptor_id", "descriptor_generation"}:
                    _fail("ANCHOR", projected, "submitted anchor has invalid shape")
            expected.append(projected)
            previous_projected = projected
            seen = filtered
        if segment_index != len(projected_ids):
            _fail("PROJECTION", name, "era retains unexplained projected commits")
        cursor = end + 1
        previous_flat = seen
        previous_descriptor = descriptor
        previous_generation = generation
    for position in range(cursor, frontier_index + 1):
        assert previous_descriptor is not None
        if _filter_by_policy(_flat(canonical, canonical_history[position], "PROJECTION"), previous_descriptor) != previous_flat:
            _fail("PROJECTION", name, "tail canonical commits were not hidden-only")
    if expected != actual or set(view["anchors"]) != set(actual):
        _fail("ANCHOR", name, "anchors and projected history differ")


def _plans(root: Path, state: dict, canonical_history: list[str], version: int) -> None:
    canonical = canonical_repo(root); index = {oid: i for i, oid in enumerate(canonical_history)}
    for identifier, job in sorted(state["projection_jobs"].items()):
        if "plan" not in job:
            if job["status"] == "ready": _fail("JOB", identifier, "ready job has no plan")
            continue
        plan = job["plan"]
        needed = {"view", "canonical_ref", "projected_ref", "old_frontier", "new_frontier", "old_tip", "new_tip", "anchors"}
        if version >= 2:
            needed |= {"descriptor_id", "descriptor_generation"}
            if plan.get("descriptor_id") != job.get("descriptor_id") or plan.get("descriptor_generation") != job.get("descriptor_generation"):
                _fail("DESCRIPTOR", identifier, "retained plan differs from the job assignment")
            view = state["views"].get(job["view"])
            if view and (plan.get("descriptor_id") != view.get("descriptor_id") or plan.get("descriptor_generation") != view.get("descriptor_generation")):
                _fail("DESCRIPTOR", identifier, "retained plan differs from the view assignment")
        if set(plan) != needed or job["status"] not in {"running", "ready", "retry"}: _fail("JOB", identifier, "plan is invalid for job state")
        view = state["views"].get(job["view"])
        if not view or any(not isinstance(plan[field], str) for field in needed - {"anchors", "descriptor_generation"}) or not isinstance(plan["anchors"], dict): _fail("JOB", identifier, "plan has invalid shape")
        if version == 2 and plan["descriptor_generation"] != 1: _fail("JOB", identifier, "plan descriptor generation must be 1")
        if version >= 3: _generation(plan["descriptor_generation"], identifier)
        if plan["view"] != job["view"] or plan["canonical_ref"] != view["canonical_ref"] or plan["projected_ref"] != view["projected_ref"] or plan["old_frontier"] != job.get("expected_frontier") or plan["old_tip"] != job.get("expected_projected_tip") or plan["new_frontier"] != job["canonical_to"] or plan["old_frontier"] != view["canonical_frontier"] or plan["old_tip"] != view["projected_tip"]: _fail("JOB", identifier, "plan cursors do not match job and view")
        if plan["old_frontier"] not in index or plan["new_frontier"] not in index or index[plan["old_frontier"]] > index[plan["new_frontier"]]: _fail("JOB", identifier, "plan frontier range is invalid")
        read_policy = _view_policy(state, job["view"], version)
        previous_tip = plan["old_tip"]; previous_visible = _filter_by_policy(_flat(canonical, plan["old_frontier"], "JOB"), read_policy); expected_keys: list[str] = []
        anchor_fields = {"canonical", "kind", "change_id"} | ({"descriptor_id", "descriptor_generation"} if version >= 2 else set())
        for oid in canonical_history[index[plan["old_frontier"]] + 1:index[plan["new_frontier"]] + 1]:
            visible_entries = _filter_by_policy(_flat(canonical, oid, "JOB"), read_policy)
            if visible != previous_visible:
                candidates = [key for key, anchor in plan["anchors"].items() if isinstance(anchor, dict) and anchor.get("canonical") == oid]
                if len(candidates) != 1: _fail("JOB", identifier, "plan anchors do not replay canonically")
                projected = candidates[0]; anchor = plan["anchors"][projected]
                if set(anchor) != anchor_fields or anchor["kind"] != "synthetic": _fail("JOB", identifier, "plan has non-synthetic anchor")
                canonical_change_id = parse_change_id(
                    _commit_parts(
                        _raw_commit(canonical, oid, "JOB", oid), "JOB", oid
                    )[3]
                )
                if anchor["change_id"] != canonical_change_id:
                    _fail("JOB", identifier, "plan Change-Id differs from canonical commit")
                raw = _raw_commit(view_repo(root, job["view"]), projected, "JOB", projected); _synthetic(raw, projected, oid, previous_tip, anchor["change_id"], "JOB")
                if _flat(view_repo(root, job["view"]), projected, "JOB") != visible_entries: _fail("JOB", identifier, "plan tree differs from filter")
                expected_keys.append(projected); previous_tip = projected
            previous_visible = visible_entries
        if set(expected_keys) != set(plan["anchors"]) or previous_tip != plan["new_tip"]: _fail("JOB", identifier, "plan tip or anchors differ from replay")


def _changes(state: dict, revisions: dict[str, list[tuple]], version: int) -> None:
    # A source publication barrier reserves its Change-Id before the submitted
    # ref and its submitted representation become durable.
    owned_field = "owner_principal_id" if version >= 2 else "owner"
    status_set = {"canonical_committed", "source_unconfirmed"}
    for tx in state["transactions"].values():
        if tx.get("kind") is None and tx["status"] in status_set:
            for mapping in tx["mappings"]:
                if mapping["change_id"]:
                    revisions.setdefault(mapping["change_id"], [])
    for tx in state["transactions"].values():
        if tx.get("kind") is None and tx["status"] in {"fanout_pending", "published"}:
            descriptor_id = tx.get("descriptor_id") if version >= 2 else None
            generation = tx.get("descriptor_generation") if version >= 2 else None
            for mapping in tx["mappings"]:
                if mapping["change_id"]: revisions.setdefault(mapping["change_id"], []).append((tx["view"], mapping["submitted"], mapping["canonical"], "submitted", descriptor_id, generation))
    if set(state["changes"]) != set(revisions): _fail("CHANGE", "changes", "logical Change-Id inventory differs from representations")
    active: dict[str, list[dict]] = {}
    for tx in state["transactions"].values():
        if tx.get("kind") is None and tx["status"] in {"canonical_committed", "source_unconfirmed", "fanout_pending", "published"}:
            for mapping in tx["mappings"]:
                if mapping["change_id"]: active.setdefault(mapping["change_id"], []).append(tx)
    for change_id, change in sorted(state["changes"].items()):
        actual: set[tuple] = set()
        expected_assignments: dict[tuple, tuple[str | None, int | None]] = {}
        for entry in revisions[change_id]:
            expected_assignments[entry[:4]] = (entry[4], entry[5])
        for revision in change["revisions"]:
            if not isinstance(revision, dict): _fail("CHANGE", change_id, "revision is not a dictionary")
            required = {"view", "projected", "canonical", "kind"}
            if version >= 2:
                required |= {"descriptor_id", "descriptor_generation"}
                if set(revision) != required | ({"submitted"} if revision["kind"] == "submitted" else set()):
                    _fail("CHANGE", change_id, "invalid revision shape")
            else:
                if not required <= set(revision) or revision["kind"] not in {"submitted", "synthetic"}: _fail("CHANGE", change_id, "invalid revision shape")
            if revision["kind"] not in {"submitted", "synthetic"}: _fail("CHANGE", change_id, "invalid revision kind")
            if revision["kind"] == "submitted" and revision.get("submitted") != revision["projected"]: _fail("CHANGE", change_id, "submitted revision lacks matching submitted ID")
            if revision["kind"] == "synthetic" and "submitted" in revision: _fail("CHANGE", change_id, "synthetic revision has submitted ID")
            key = (revision["view"], revision["projected"], revision["canonical"], revision["kind"])
            if key in actual: _fail("CHANGE", change_id, "duplicate revision")
            actual.add(key)
            if version >= 2 and key in expected_assignments:
                descriptor_id, generation = expected_assignments[key]
                if revision["descriptor_id"] != descriptor_id or revision["descriptor_generation"] != generation:
                    _fail("CHANGE", change_id, "revision descriptor differs from its anchor")
        if actual != {entry[:4] for entry in revisions[change_id]}: _fail("CHANGE", change_id, "revisions differ from anchors and mappings")
        owners = active.get(change_id, [])
        if owners:
            if change.get(owned_field) is None or change.get("canonical_ref") is None or any(change[owned_field] != tx["principal_id" if version >= 2 else "principal"] or change["canonical_ref"] != tx["canonical_ref"] for tx in owners): _fail("CHANGE", change_id, "ownership differs from source transaction")
        elif owned_field in change or "canonical_ref" in change:
            _fail("CHANGE", change_id, "unbacked ownership reservation")


def _outbox_jobs(state: dict, canonical_history: list[str], version: int) -> None:
    events_by_tx: dict[str, dict] = {}
    for identifier, event in sorted(state["outbox"].items()):
        tx = state["transactions"].get(event["transaction_id"])
        mismatch = not tx or event["id"] != event_id(event["transaction_id"]) or event["type"] != "canonical.committed" or event["sequence"] != tx["sequence"] or event["canonical_to"] != tx["canonical_new"] or len(event["target_views"]) != len(set(event["target_views"])) or any(view not in state["views"] for view in event["target_views"]) or (tx.get("kind") is None and tx["view"] in event["target_views"]) or not set(tx["pending_views"]).issubset(event["target_views"])
        if version >= 2 and not mismatch:
            mismatch = (
                event.get("target_descriptors") != tx.get("target_descriptors")
                or event["target_views"] != sorted(event["target_descriptors"])
            )
        if mismatch: _fail("OUTBOX", identifier, "event does not match transaction")
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
        mismatch = not event or not tx or event["transaction_id"] != job["transaction_id"] or job["id"] != job_id(job["transaction_id"], job["view"]) or job["event_id"] != event["id"] or job["view"] not in event["target_views"] or job["sequence"] != event["sequence"] or job["sequence"] != tx["sequence"] or job["canonical_to"] != event["canonical_to"] or job["canonical_to"] != tx["canonical_new"]
        if version >= 2 and not mismatch:
            assignment = event.get("target_descriptors", {}).get(job["view"], {})
            mismatch = job.get("descriptor_id") != assignment.get("descriptor_id") or job.get("descriptor_generation") != assignment.get("descriptor_generation")
        if mismatch: _fail("JOB", identifier, "job does not match event and transaction")
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


def _reachability(root: Path, state: dict, view_closures: dict[str, tuple[set[str], set[str], set[str]]]) -> None:
    for name, view in sorted(state["views"].items()):
        repo = view_repo(root, name)
        allowed_commits, allowed_trees, allowed_blobs = view_closures[name]
        if git(repo, "fsck", "--full", "--strict", "--no-reflogs", "--no-dangling", check=False).returncode: _fail("REACHABILITY", name, "strict fsck failed")
        result = git(repo, "rev-list", "--objects", "--no-object-names", view["projected_tip"], check=False)
        if result.returncode: _fail("REACHABILITY", name, "cannot enumerate closure")
        for raw_oid in result.stdout.splitlines():
            try:
                oid = raw_oid.decode("ascii")
            except UnicodeDecodeError:
                _fail("REACHABILITY", repr(raw_oid), "closure contains a non-ASCII object ID")
            if not _oid(oid):
                _fail("REACHABILITY", repr(raw_oid), "closure contains an invalid object ID")
            kind = git(repo, "cat-file", "-t", oid, check=False)
            if kind.returncode: _fail("REACHABILITY", oid, "missing local object")
            kind_str = kind.stdout.strip()
            if kind_str == b"commit":
                if oid not in allowed_commits:
                    _fail("REACHABILITY", oid, "reachable commit is not in anchored projected chain")
            elif kind_str == b"tree":
                if oid not in allowed_trees:
                    _fail("REACHABILITY", oid, "reachable tree is not in projected snapshot")
            elif kind_str == b"blob":
                if oid not in allowed_blobs:
                    _fail("REACHABILITY", oid, "reachable blob is not in a projected leaf")
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


def _schema_dispatch(state: dict) -> None:
    version = state.get("version")
    if version == 1 and not isinstance(version, bool):
        _schema_v1(state)
    elif version == 2 and not isinstance(version, bool):
        _schema_v2(state)
    elif version == 3 and not isinstance(version, bool):
        _schema_v3(state)
    else:
        _fail("STATE_SCHEMA", "state", f"unsupported forge state version: {version!r}")


def _identity_and_descriptor_checks(state: dict) -> None:
    _phase("IDENTITY", lambda: _schema_catalog(state, state["version"]))
    _phase("DESCRIPTOR", lambda: _identity_resolution_checks(state))


def _history_phases(
    root: Path, state: dict, canonical_refs: dict, view_refs: dict
) -> None:
    version = state["version"]
    tip = _phase("CANONICAL_REF", lambda: _check_refs(root, state, canonical_refs, view_refs))
    canonical_history = _phase("CANONICAL_GRAPH", lambda: _canonical_graph(root, tip))
    _phase("FRONTIER", lambda: _policy_frontiers(root, state, canonical_history, tip, version))
    _phase("MAPPING", lambda: _source_transactions(root, state, canonical_history, tip, version))
    _phase("CANONICAL_REF", lambda: _admin_transactions(root, state, canonical_history, tip))
    revisions, view_closures = _phase("PROJECTION", lambda: _projected(root, state, canonical_history, version))
    _phase("JOB", lambda: _plans(root, state, canonical_history, version))
    _phase("CHANGE", lambda: _changes(state, revisions, version))
    _phase("OUTBOX", lambda: _outbox_jobs(state, canonical_history, version))
    _phase("REACHABILITY", lambda: _reachability(root, state, view_closures))


def check_loaded(root: Path, state: dict, canonical_refs: dict, view_refs: dict) -> None:
    """Full invariant check of an already-loaded state against collected refs.

    Used by migration: acquires no lock and never reloads ``state.json``.
    """
    _phase("STATE_SCHEMA", lambda: _schema_dispatch(state))
    if state["version"] >= 2:
        _identity_and_descriptor_checks(state)
    _history_phases(root, state, canonical_refs, view_refs)


def collect_layout(root: Path, state: dict) -> tuple[dict, dict]:
    """Collect the repository and ref inventory without checking schemas."""
    return _layout_and_refs(root, state)


def check_invariants(root: Path) -> None:
    """Raise ForgeError if the forge violates a global invariant."""
    lock_path = root / "state.lock"
    try:
        with lock_path.open("r") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = copy.deepcopy(load_state(root))
            _phase("STATE_SCHEMA", lambda: _schema_dispatch(state))
            if state["version"] >= 2:
                _identity_and_descriptor_checks(state)
            canonical_refs, view_refs = _phase("FORGE_LAYOUT", lambda: _layout_and_refs(root, state))
    except ForgeError:
        raise
    except OSError as error:
        _fail("STATE_SCHEMA", "state", f"cannot acquire snapshot: {error}")
    _history_phases(root, state, canonical_refs, view_refs)
