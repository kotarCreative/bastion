"""Compatibility and integrity tests for permission-bearing descriptors."""

import copy
import hashlib
import json

import pytest

import identity
import wal
from errors import ForgeError
from permissions import (
    BRANCH_ACTIONS,
    combine_permissions,
    descriptor_permissions,
    empty_permissions,
    repository_allowed,
)


def catalog():
    return {
        "canonical_branch": "main",
        "principals": {"user": {"id": "user", "kind": "user"}},
        "teams": {},
        "roles": {},
        "role_bindings": {},
        "view_descriptors": {},
        "views": {},
    }


def legacy_payload():
    caps = identity.empty_capability_map()
    return {
        "algorithm": "capabilities-v3",
        "principal_id": "user",
        "principal_kind": "user",
        "canonical_ref": "refs/heads/main",
        "team_ids": [],
        "role_ids": [],
        "binding_ids": [],
        "capabilities": caps,
        "restrictions": copy.deepcopy(caps),
    }


def reference_id(payload):
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return "vdesc-" + hashlib.sha256(b"bastion-effective-view\0" + encoded).hexdigest()


def test_legacy_descriptor_payload_id_and_role_storage_are_unchanged():
    state = catalog()
    before = identity.effective_descriptor(state, "user", "refs/heads/main")
    expected = legacy_payload()
    assert {key: value for key, value in before.items() if key != "id"} == expected
    assert before["id"] == reference_id(expected)

    allow = identity.empty_capability_map()
    deny = identity.empty_capability_map()
    allow["read"] = ["src"]
    identity.add_role(state, "legacy", allow, deny)
    role = state["roles"]["legacy"]
    assert set(role) == {"id", "allow", "deny"}
    assert role["allow"]["read"] == ["src"]
    assert "permissions" not in role


def test_permission_roles_and_descriptor_union_change_hashed_content():
    state = catalog()
    allow = identity.empty_capability_map()
    deny = identity.empty_capability_map()
    allow["read"] = ["src"]
    legacy_role = identity.add_role(state, "legacy", allow, deny)
    assert legacy_role is None
    identity.bind_role(state, "legacy", "principal", "user")
    old_descriptor = identity.effective_descriptor(state, "user", "refs/heads/main")

    policy = empty_permissions()
    policy["repository"]["allow"] = ["pr_view", "pr_view"]
    identity.add_role(state, "new", identity.empty_capability_map(), identity.empty_capability_map(), permissions=policy)
    assert state["roles"]["new"]["permissions"]["repository"]["allow"] == ["pr_view"]
    identity.bind_role(state, "new", "principal", "user")
    current = identity.effective_descriptor(state, "user", "refs/heads/main")
    assert current["algorithm"] == identity.PERMISSIONS_DESCRIPTOR_ALGORITHM
    assert current["permissions"]["repository"]["allow"] == ["pr_view"]
    assert current["id"] != old_descriptor["id"]
    assert current["id"] == reference_id({key: value for key, value in current.items() if key != "id"})
    assert wal.canonical_json(current) == json.dumps(current, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def test_permission_only_and_deny_only_roles_work_but_empty_role_does_not():
    state = catalog()
    empty = identity.empty_capability_map()
    allow = empty_permissions()
    allow["repository"]["allow"] = ["pr_create"]
    identity.add_role(state, "allow-only", empty, identity.empty_capability_map(), permissions=allow)
    deny = empty_permissions()
    deny["repository"]["deny"] = ["pr_merge"]
    identity.add_role(state, "deny-only", identity.empty_capability_map(), identity.empty_capability_map(), permissions=deny)
    with pytest.raises(ForgeError):
        identity.add_role(state, "empty", identity.empty_capability_map(), identity.empty_capability_map(), permissions=empty_permissions())


def test_service_account_owner_does_not_inherit_permissions():
    state = catalog()
    identity.add_service_account(state, "bot", "user")
    policy = empty_permissions()
    policy["repository"]["allow"] = ["pr_view"]
    identity.add_role(state, "view", identity.empty_capability_map(), identity.empty_capability_map(), permissions=policy)
    identity.bind_role(state, "view", "principal", "user")
    owner = identity.effective_descriptor(state, "user", "refs/heads/main")
    bot = identity.effective_descriptor(state, "bot", "refs/heads/main")
    assert repository_allowed(owner["permissions"], "pr_view")
    assert not repository_allowed(descriptor_permissions(bot), "pr_view")


def test_descriptor_permissions_legacy_default_and_v4_normalization():
    v3 = {"algorithm": "capabilities-v3"}
    assert descriptor_permissions(v3) == empty_permissions()
    injected = dict(v3, permissions=empty_permissions())
    with pytest.raises(ForgeError):
        descriptor_permissions(injected)
    with pytest.raises(ForgeError):
        descriptor_permissions({"algorithm": "future"})

    policy = empty_permissions()
    policy["branches"]["allow"]["read"] = ["refs/heads/*"]
    v4 = {"algorithm": "capabilities-v4", "permissions": policy}
    assert descriptor_permissions(v4) == policy


def test_wal_rejects_malformed_v3_v4_and_tampered_descriptors():
    payload = legacy_payload()
    legacy = {"id": reference_id(payload), **payload}
    assert wal.validate_descriptor(legacy) == legacy
    for bad in (dict(legacy, permissions=empty_permissions()), dict(legacy, algorithm="future")):
        with pytest.raises(ForgeError):
            wal.validate_descriptor(bad)

    policy = empty_permissions()
    policy["repository"]["allow"] = ["pr_view"]
    payload4 = dict(payload, algorithm="capabilities-v4", permissions=policy)
    descriptor = {"id": reference_id(payload4), **payload4}
    assert wal.validate_descriptor(descriptor) == descriptor
    malformed = []
    malformed.append({key: value for key, value in descriptor.items() if key != "permissions"})
    bad_policy = copy.deepcopy(policy)
    bad_policy["repository"]["allow"] = ["not-an-action"]
    malformed.append(dict(descriptor, permissions=bad_policy))
    bad_policy = copy.deepcopy(policy)
    bad_policy["repository"]["allow"] = ["pr_view", "pr_view"]
    malformed.append(dict(descriptor, permissions=bad_policy))
    bad_policy = copy.deepcopy(policy)
    bad_policy["repository"]["allow"] = ["pr_view", "pr_create"]
    bad_policy["repository"]["allow"].reverse()
    malformed.append(dict(descriptor, permissions=bad_policy))
    malformed.append(dict(descriptor, id="vdesc-" + "0" * 64))
    for bad in malformed:
        with pytest.raises(ForgeError):
            wal.validate_descriptor(bad)


def test_frozen_runtime_policies_allow_tuple_sequences_but_wal_json_is_strict():
    from types import MappingProxyType

    frozen = MappingProxyType({
        "repository": MappingProxyType({"allow": ("pr_view",), "deny": ()}),
        "branches": MappingProxyType({
            polarity: MappingProxyType({action: () for action in BRANCH_ACTIONS})
            for polarity in ("allow", "deny")
        }),
    })
    assert combine_permissions([frozen])["repository"]["allow"] == ["pr_view"]
    payload = legacy_payload()
    policy = empty_permissions()
    policy["repository"]["allow"] = ["pr_view"]
    payload4 = dict(payload, algorithm="capabilities-v4", permissions=policy)
    frozen_record = {"id": reference_id(payload4), **payload4}
    frozen_record["permissions"] = frozen
    with pytest.raises(ForgeError):
        wal.validate_descriptor(frozen_record)
