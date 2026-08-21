"""Immutable identity catalog and effective-view-descriptor resolution."""

from __future__ import annotations

import hashlib
import json
import re

from errors import ForgeError
from validation import normalize_prefix


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
PRINCIPAL_KINDS = {"user", "service_account"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CAPABILITIES = (
    "read",
    "create",
    "modify",
    "delete",
    "mode",
    "symlink",
    "branch",
    "merge",
    "admin",
    "tag",
)
DESCRIPTOR_ALGORITHM = "capabilities-v2"
LEGACY_DESCRIPTOR_ALGORITHM = "effective-view-v1"
DESCRIPTOR_DOMAIN = b"bastion-effective-view-v1\0"
TIMESTAMP_SKEW_SECONDS = 600


def validate_identifier(identifier: str) -> None:
    if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
        raise ForgeError(f"identifier must be a simple 1-64 character token: {identifier!r}")


def normalize_emails(values: object) -> list[str]:
    """Validate, lowercase, sort, and deduplicate declared author addresses."""
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ForgeError("emails must be a list of address strings")
    normalized: set[str] = set()
    for value in values:
        if len(value) > 254 or not EMAIL.fullmatch(value.strip()):
            raise ForgeError(f"invalid email address: {value!r}")
        normalized.add(value.strip().lower())
    return sorted(normalized)


def prefix_covers(ancestor: str, descendant: str) -> bool:
    """Whether one normalized prefix covers another at path-component boundaries."""
    if ancestor == "":
        return True
    return descendant == ancestor or descendant.startswith(ancestor + "/")


def minimize_prefixes(values: list[str]) -> list[str]:
    """Sort, deduplicate, and drop prefixes covered by a retained ancestor."""
    kept: list[str] = []
    for value in sorted(set(values)):
        if any(prefix_covers(retained, value) for retained in kept):
            continue
        kept.append(value)
    return kept


def empty_capability_map() -> dict[str, list[str]]:
    return {capability: [] for capability in CAPABILITIES}


def intersect_prefixes(read: list[str], write: list[str]) -> list[str]:
    """Component-wise read/write intersection keeping the narrower area."""
    result: set[str] = set()
    for reader in read:
        for writer in write:
            if prefix_covers(reader, writer):
                result.add(writer)
            elif prefix_covers(writer, reader):
                result.add(reader)
    return minimize_prefixes(sorted(result))


def normalize_capability_map(raw: object, subject: str) -> dict[str, list[str]]:
    """Validate and normalize one allow or deny map with all nine capabilities."""
    if not isinstance(raw, dict):
        raise ForgeError(f"{subject} must be a capability map")
    if set(raw) != set(CAPABILITIES):
        raise ForgeError(f"{subject} must hold exactly the nine capabilities")
    result = empty_capability_map()
    for capability in CAPABILITIES:
        values = raw[capability]
        if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
            raise ForgeError(f"{subject}.{capability} must be a list of path strings")
        result[capability] = minimize_prefixes(
            [normalize_prefix(value) for value in values]
        )
    return result


def add_principal(state: dict, principal_id: str, emails: list[str] | None = None) -> None:
    validate_identifier(principal_id)
    principals = state.setdefault("principals", {})
    if principal_id in principals:
        raise ForgeError(f"principal already exists: {principal_id}")
    record: dict = {"id": principal_id, "kind": "user"}
    if emails:
        record["emails"] = normalize_emails(emails)
    principals[principal_id] = record


def add_service_account(
    state: dict,
    service_id: str,
    owner_principal_id: str,
    emails: list[str] | None = None,
) -> None:
    validate_identifier(service_id)
    validate_identifier(owner_principal_id)
    principals = state.setdefault("principals", {})
    if service_id in principals:
        raise ForgeError(f"principal already exists: {service_id}")
    owner = principals.get(owner_principal_id)
    if owner is None:
        raise ForgeError(f"unknown owner principal: {owner_principal_id}")
    if owner["kind"] != "user":
        raise ForgeError("service-account owner must be a user principal")
    record: dict = {
        "id": service_id,
        "kind": "service_account",
        "owner_principal_id": owner_principal_id,
    }
    if emails:
        record["emails"] = normalize_emails(emails)
    principals[service_id] = record


def add_team(state: dict, team_id: str, members: list[str]) -> None:
    validate_identifier(team_id)
    teams = state.setdefault("teams", {})
    if team_id in teams:
        raise ForgeError(f"team already exists: {team_id}")
    unique = sorted(set(members))
    for member in unique:
        validate_identifier(member)
        record = state.setdefault("principals", {}).get(member)
        if record is None:
            raise ForgeError(f"unknown team member principal: {member}")
        if record["kind"] not in PRINCIPAL_KINDS:
            raise ForgeError(f"invalid principal kind for member: {member}")
    teams[team_id] = {"id": team_id, "members": unique}


def add_role(
    state: dict,
    role_id: str,
    allow: dict[str, list[str]],
    deny: dict[str, list[str]],
) -> None:
    validate_identifier(role_id)
    roles = state.setdefault("roles", {})
    if role_id in roles:
        raise ForgeError(f"role already exists: {role_id}")
    normalized_allow = normalize_capability_map(allow, "allow")
    normalized_deny = normalize_capability_map(deny, "deny")
    if not any(normalized_allow[capability] for capability in CAPABILITIES):
        raise ForgeError("a role needs at least one allow grant")
    roles[role_id] = {
        "id": role_id,
        "allow": normalized_allow,
        "deny": normalized_deny,
    }


def binding_identifier(subject_kind: str, subject_id: str, role_id: str) -> str:
    return f"{subject_kind}:{subject_id}:{role_id}"


def bind_role(state: dict, role_id: str, subject_kind: str, subject_id: str) -> str:
    if subject_kind not in ("principal", "team"):
        raise ForgeError(f"invalid binding subject kind: {subject_kind}")
    validate_identifier(subject_id)
    validate_identifier(role_id)
    bindings = state.setdefault("role_bindings", {})
    identifier = binding_identifier(subject_kind, subject_id, role_id)
    if identifier in bindings:
        raise ForgeError(f"role binding already exists: {identifier}")
    if role_id not in state.setdefault("roles", {}):
        raise ForgeError(f"unknown role: {role_id}")
    if subject_kind == "principal":
        if subject_id not in state.setdefault("principals", {}):
            raise ForgeError(f"unknown principal: {subject_id}")
    else:
        team = state.setdefault("teams", {}).get(subject_id)
        if team is None:
            raise ForgeError(f"unknown team: {subject_id}")
    bindings[identifier] = {
        "id": identifier,
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "role_id": role_id,
    }
    return identifier


def require_author_binding(
    state: dict, principal_id: str, author_email: bytes
) -> None:
    """Enforce declared author identities for one submitted commit."""
    record = state.setdefault("principals", {}).get(principal_id)
    if record is None:
        raise ForgeError(f"unknown principal: {principal_id}")
    declared = record.get("emails") or []
    if not declared:
        return
    address = author_email.decode(errors="surrogateescape").strip().lower()
    if address not in declared:
        raise ForgeError("submission is authored by an undeclared identity")


def resolve_principal(state: dict, principal_id: str) -> tuple[list[str], list[str], list[str]]:
    """Return sorted direct teams, applicable bindings, and referenced roles."""
    principal = state.setdefault("principals", {}).get(principal_id)
    if principal is None:
        raise ForgeError(f"unknown principal: {principal_id}")
    teams = state.setdefault("teams", {})
    bindings = state.setdefault("role_bindings", {})
    team_ids = sorted(key for key, team in teams.items() if principal_id in team["members"])
    selected = sorted(
        key
        for key, binding in bindings.items()
        if (binding["subject_kind"] == "principal" and binding["subject_id"] == principal_id)
        or (binding["subject_kind"] == "team" and binding["subject_id"] in set(team_ids))
    )
    role_ids: set[str] = set()
    for key in selected:
        binding = bindings[key]
        if binding["role_id"] not in state.setdefault("roles", {}):
            raise ForgeError(f"binding references an unknown role: {key}")
        if binding["subject_kind"] == "principal" and binding["subject_id"] != principal_id:
            raise ForgeError(f"selected binding does not apply to the principal: {key}")
        if binding["subject_kind"] == "team" and (
            binding["subject_id"] not in state["teams"]
            or principal_id not in state["teams"][binding["subject_id"]]["members"]
        ):
            raise ForgeError(f"selected binding does not apply to the principal: {key}")
        role_ids.add(binding["role_id"])
    return team_ids, selected, sorted(role_ids)


def effective_grants(
    state: dict, role_ids: list[str]
) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Union role grants additively per capability, separately for allow and deny."""
    allow_union = empty_capability_map()
    deny_union = empty_capability_map()
    for role_id in role_ids:
        role = state["roles"].get(role_id)
        if role is None:
            raise ForgeError(f"unknown role: {role_id}")
        for capability in CAPABILITIES:
            allow_union[capability].extend(role["allow"][capability])
            deny_union[capability].extend(role["deny"][capability])
    effective_allow = empty_capability_map()
    effective_deny = empty_capability_map()
    for capability in CAPABILITIES:
        effective_allow[capability] = minimize_prefixes(allow_union[capability])
        effective_deny[capability] = minimize_prefixes(deny_union[capability])
    return effective_allow, effective_deny


def descriptor_payload(
    state: dict,
    principal_id: str,
    canonical_ref: str,
    team_ids: list[str],
    binding_ids: list[str],
    role_ids: list[str],
) -> dict:
    principal = state["principals"][principal_id]
    allow, deny = effective_grants(state, role_ids)
    return {
        "algorithm": DESCRIPTOR_ALGORITHM,
        "principal_id": principal_id,
        "principal_kind": principal["kind"],
        "canonical_ref": canonical_ref,
        "team_ids": team_ids,
        "role_ids": role_ids,
        "binding_ids": binding_ids,
        "capabilities": allow,
        "restrictions": deny,
    }


def compute_descriptor_id(payload: dict) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return "vdesc-" + hashlib.sha256(DESCRIPTOR_DOMAIN + encoded).hexdigest()


def effective_descriptor(state: dict, principal_id: str, canonical_ref: str) -> dict:
    """Resolve or reuse the immutable descriptor for one principal and ref."""
    configured = f"refs/heads/{state['canonical_branch']}"
    if canonical_ref != configured:
        raise ForgeError("descriptor requires the configured canonical branch")
    team_ids, binding_ids, role_ids = resolve_principal(state, principal_id)
    payload = descriptor_payload(
        state, principal_id, canonical_ref, team_ids, binding_ids, role_ids
    )
    identifier = compute_descriptor_id(payload)
    record = {"id": identifier}
    record.update(payload)
    existing = state.setdefault("view_descriptors", {}).get(identifier)
    if existing is not None:
        if existing != record:
            raise ForgeError("descriptor identity collision with different content")
        return existing
    state["view_descriptors"][identifier] = record
    return record


def require_descriptor_assignment(view: dict) -> tuple[str, int]:
    descriptor_id = view.get("descriptor_id")
    generation = view.get("descriptor_generation")
    if not isinstance(descriptor_id, str):
        raise ForgeError("view lacks a valid descriptor assignment")
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise ForgeError("view lacks a valid descriptor generation")
    return descriptor_id, generation


def require_matching_descriptor(
    view: dict, descriptor_id: str, descriptor_generation: int
) -> None:
    current_id, current_generation = require_descriptor_assignment(view)
    if current_id != descriptor_id or current_generation != descriptor_generation:
        raise ForgeError("projection descriptor changed")


def assigned_descriptor(state: dict, principal_id: str) -> dict | None:
    """The descriptor currently bound to this principal's views, if any."""
    for view in state.setdefault("views", {}).values():
        if view["principal_id"] == principal_id:
            descriptor = state["view_descriptors"].get(view["descriptor_id"])
            if descriptor is None:
                raise ForgeError("view references an unknown descriptor")
            return descriptor
    return None


def _verified_descriptor_record(state: dict, descriptor_id: str) -> dict:
    descriptor = state["view_descriptors"].get(descriptor_id)
    if descriptor is None:
        raise ForgeError(f"unknown descriptor: {descriptor_id}")
    payload = {key: value for key, value in descriptor.items() if key != "id"}
    if compute_descriptor_id(payload) != descriptor_id:
        raise ForgeError("descriptor content does not match its identity")
    if payload.get("algorithm") != DESCRIPTOR_ALGORITHM:
        raise ForgeError(f"unsupported descriptor algorithm: {payload.get('algorithm')!r}")
    return descriptor


def descriptor_for_view(state: dict, view_name: str) -> dict:
    """Load the immutable descriptor bound to one physical view.

    Catalog additions are dormant until an explicit policy transition, so
    the bound descriptor is served verbatim; only its integrity is checked.
    """
    view = state.setdefault("views", {}).get(view_name)
    if view is None:
        raise ForgeError(f"unknown view: {view_name}")
    descriptor_id, generation = require_descriptor_assignment(view)
    descriptor = _verified_descriptor_record(state, descriptor_id)
    if view["canonical_ref"] != descriptor["canonical_ref"]:
        raise ForgeError(f"descriptor canonical ref differs from view: {view_name}")
    if descriptor["principal_id"] != view["principal_id"]:
        raise ForgeError(f"descriptor principal differs from view: {view_name}")
    if generation < 1:
        raise ForgeError(f"unsupported descriptor generation: {generation}")
    return descriptor
