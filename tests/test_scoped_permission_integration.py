"""Cross-layer checks for repository/branch permission policy activation."""

from __future__ import annotations

import subprocess

import forge
import identity
from invariants import check_invariants
from permissions import (
    branch_allowed,
    descriptor_permissions,
    empty_permissions,
    repository_allowed,
)
from repository import Repository
from service import (
    add_principal,
    add_view,
    add_role,
    add_team,
    bind_role,
    transition_principal,
)
from state import load_state
from store import backup_forge, restore_forge


def _forge(tmp_path):
    source = tmp_path / "source"
    source.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=source, check=True)
    (source / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=source, check=True)
    root = tmp_path / "forge"
    forge.init_forge(root, source, "main")
    return root


def _permission_role(root, role_id, *, repo_allow=(), repo_deny=(), branch_allow=(), branch_deny=()):
    policy = empty_permissions()
    policy["repository"]["allow"] = list(repo_allow)
    policy["repository"]["deny"] = list(repo_deny)
    policy["branches"]["allow"]["read"] = list(branch_allow)
    policy["branches"]["deny"]["read"] = list(branch_deny)
    return add_role(
        root, role_id, identity.empty_capability_map(), identity.empty_capability_map(),
        permissions=policy,
    )


def test_policy_is_dormant_until_transition_and_pinned_snapshot_is_immutable(tmp_path):
    root = _forge(tmp_path)
    add_principal(root, "alice")
    path_allow = identity.empty_capability_map()
    path_allow["read"] = [""]
    add_role(root, "alice-path", path_allow, identity.empty_capability_map())
    bind_role(root, "alice-path", "principal", "alice")
    add_view(root, "alice", "alice", "main")
    _permission_role(root, "reviewer", repo_allow=["pr_view"], branch_allow=["refs/heads/*"])
    bind_role(root, "reviewer", "principal", "alice")

    repository = Repository.open(root)
    before = repository.load()[2]
    old_descriptor = repository._descriptor(before.active_views["alice"]["descriptor_id"])
    assert old_descriptor["algorithm"] == "capabilities-v3"
    transition_principal(root, "alice")
    active = repository.load()[2]
    descriptor = repository._descriptor(active.active_views["alice"]["descriptor_id"])
    assert descriptor["algorithm"] == "capabilities-v4"
    assert repository_allowed(descriptor_permissions(descriptor), "pr_view")
    assert branch_allowed(descriptor_permissions(descriptor), "read", "refs/heads/topic")

    _permission_role(root, "revoke", repo_deny=["pr_view"])
    bind_role(root, "revoke", "principal", "alice")
    # The active view and its captured descriptor do not change on catalog edits.
    still_active = repository.load()[2]
    assert still_active.active_views["alice"]["descriptor_id"] == descriptor["id"]
    assert repository._descriptor(descriptor["id"]) == descriptor
    assert repository_allowed(descriptor_permissions(repository._descriptor(descriptor["id"])), "pr_view")

    transition_principal(root, "alice")
    revoked = repository.load()[2]
    revoked_descriptor = repository._descriptor(revoked.active_views["alice"]["descriptor_id"])
    assert not repository_allowed(descriptor_permissions(revoked_descriptor), "pr_view")
    # A caller holding an older replay generation retains its original policy.
    assert before.active_views["alice"]["descriptor_id"] == old_descriptor["id"]
    assert repository_allowed(descriptor_permissions(descriptor), "pr_view")
    assert check_invariants(root) is None


def test_branch_grants_are_explicit_specific_and_survive_backup_restore(tmp_path):
    root = _forge(tmp_path)
    add_principal(root, "reader")
    path_allow = identity.empty_capability_map()
    path_allow["read"] = [""]
    add_role(root, "reader-path", path_allow, identity.empty_capability_map())
    bind_role(root, "reader-path", "principal", "reader")
    add_view(root, "reader", "reader", "main")
    # A branch-only role is valid; it grants no repository operation.
    _permission_role(
        root, "branch-reader", branch_allow=["refs/heads/*", "refs/heads/feature/*"],
        branch_deny=["refs/heads/feature/private"],
    )
    bind_role(root, "branch-reader", "principal", "reader")
    transition_principal(root, "reader")
    repository = Repository.open(root)
    snapshot = repository.load()[2]
    descriptor = repository._descriptor(snapshot.active_views["reader"]["descriptor_id"])
    permissions = descriptor_permissions(descriptor)
    assert not repository_allowed(permissions, "pr_view")
    assert branch_allowed(permissions, "read", "refs/heads/feature/public")
    assert not branch_allowed(permissions, "read", "refs/heads/feature/private")
    assert branch_allowed(permissions, "read", "refs/heads/other/topic")

    backup = tmp_path / "backup"
    restored = tmp_path / "restored"
    backup_forge(root, backup)
    restore_forge(backup, restored)
    restored_repo = Repository.open(restored)
    replay = restored_repo.load()[2]
    restored_descriptor = restored_repo._descriptor(replay.active_views["reader"]["descriptor_id"])
    assert restored_descriptor == descriptor
    assert descriptor_permissions(restored_descriptor) == permissions
    check_invariants(restored)


def test_permissions_do_not_leak_between_repositories_with_same_principal_id(tmp_path):
    left = _forge(tmp_path / "left")
    right = _forge(tmp_path / "right")
    for root in (left, right):
        add_principal(root, "shared-user")
    _permission_role(left, "left-only", repo_allow=["pr_create"])
    bind_role(left, "left-only", "principal", "shared-user")

    from identity import effective_descriptor

    left_policy = descriptor_permissions(effective_descriptor(load_state(left), "shared-user", "refs/heads/main"))
    right_policy = descriptor_permissions(effective_descriptor(load_state(right), "shared-user", "refs/heads/main"))
    assert repository_allowed(left_policy, "pr_create")
    assert not repository_allowed(right_policy, "pr_create")
    assert not repository_allowed(left_policy, "pr_merge")
    assert not repository_allowed(right_policy, "pr_view")
