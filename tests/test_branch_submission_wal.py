"""Durable branch submission replay contracts."""

import hashlib
import tempfile
from pathlib import Path

import identity
import pytest
from errors import ForgeError
from object_store_memory import MemoryObjectStore
from permissions import authorized_branch_refs, empty_permissions, transport_branch_allowed
from repository import Candidate, Repository
from wal import canonical_json, record_fingerprint


ZERO = "0" * 40
TIP = "1" * 40


def v4_descriptor(policy):
    payload = {
        "algorithm": identity.PERMISSIONS_DESCRIPTOR_ALGORITHM,
        "principal_id": "alice", "principal_kind": "user",
        "canonical_ref": "refs/heads/main", "team_ids": [], "role_ids": [],
        "binding_ids": [], "capabilities": identity.empty_capability_map(),
        "restrictions": identity.empty_capability_map(), "permissions": policy,
    }
    return {"id": "vdesc-" + hashlib.sha256(b"bastion-effective-view\0" + canonical_json(payload)).hexdigest(), **payload}


def fixture():
    policy = empty_permissions()
    for action in ("read", "create", "update"):
        policy["branches"]["allow"][action] = ["refs/heads/*"]
    descriptor = v4_descriptor(policy)
    repo = Repository(Path(tempfile.mkdtemp()), MemoryObjectStore(), "a" * 32)
    repo.put_descriptor(descriptor)
    imported = {"canonical_ref": "refs/heads/main", "canonical_tip": TIP,
                "canonical_pack": {"kind": "canonical", "pack_key": "packs/canonical/" + "2" * 64 + ".pack",
                    "pack_sha256": "2" * 64, "pack_git_checksum": "2" * 40, "pack_size": 1,
                    "idx_key": "packs/canonical/" + "2" * 64 + ".idx", "idx_sha256": "2" * 64,
                    "idx_size": 1, "object_count": 1}, "change_claims": []}
    repo.bootstrap(Candidate(record_fingerprint(repo.repository_id, "import", imported), imported))
    view = {"view_id": "v1", "principal_id": "alice", "projected_ref": "refs/heads/main",
            "descriptor_id": descriptor["id"], "descriptor_generation": 1,
            "projection_epoch": 1, "canonical_frontier": TIP}
    repo.publish(lambda manifest, state: Candidate(record_fingerprint(repo.repository_id, "view_create", view), view))
    return repo, descriptor


def test_transport_permissions_are_versioned_explicit_and_sorted():
    policy = empty_permissions()
    policy["branches"]["allow"]["read"] = ["refs/heads/z", "refs/heads/topic/*"]
    policy["branches"]["allow"]["create"] = ["refs/heads/z"]
    descriptor = {"algorithm": "capabilities-v4", "canonical_ref": "refs/heads/main", "permissions": policy}
    refs = {"refs/heads/z": TIP, "refs/heads/topic/a": TIP, "refs/heads/main": TIP}
    assert authorized_branch_refs(descriptor, refs) == ["refs/heads/topic/a", "refs/heads/z"]
    assert transport_branch_allowed(descriptor, "create", "refs/heads/z")
    legacy = {"algorithm": "capabilities-v3", "canonical_ref": "refs/heads/main"}
    assert transport_branch_allowed(legacy, "update", "refs/heads/main")
    assert not transport_branch_allowed(legacy, "read", "refs/heads/z")
    assert not transport_branch_allowed(legacy, "create", "refs/heads/main")


def pure_fork(repo, descriptor, *, target="refs/heads/topic", source="refs/heads/main", **changes):
    payload = {"idempotency_key_hash": None, "view_id": "v1", "principal_id": "alice",
        "descriptor_id": descriptor["id"], "descriptor_generation": 1, "projection_epoch": 1,
        "canonical_ref": target, "canonical_old": TIP, "canonical_new": TIP,
        "projected_ref": target, "projected_old": TIP, "projected_new": TIP,
        "canonical_frontier_old": TIP, "canonical_pack": None, "submission_pack": None,
        "mappings": [], "received_at": 1, "transport": "ssh", "remote_identity": None,
        "source_ref": source, "receive_old": ZERO}
    payload.update(changes)
    return repo.publish(lambda manifest, state: Candidate(
        record_fingerprint(repo.repository_id, "branch_submission", payload), payload))


def advance_ref(repo, descriptor, target, old, new, submitted):
    canonical_seed = new[-1]
    submitted_seed = submitted[-1]
    canonical_pack = {"kind": "canonical", "pack_key": "packs/canonical/" + canonical_seed * 64 + ".pack",
        "pack_sha256": canonical_seed * 64, "pack_git_checksum": canonical_seed * 40, "pack_size": 1,
        "idx_key": "packs/canonical/" + canonical_seed * 64 + ".idx", "idx_sha256": canonical_seed * 64,
        "idx_size": 1, "object_count": 1}
    submitted_pack = {**canonical_pack, "kind": "submission",
        "pack_key": "packs/submissions/" + submitted_seed * 64 + ".pack",
        "idx_key": "packs/submissions/" + submitted_seed * 64 + ".idx",
        "pack_sha256": submitted_seed * 64, "pack_git_checksum": submitted_seed * 40,
        "idx_sha256": submitted_seed * 64}
    payload = {"idempotency_key_hash": None, "view_id": "v1", "principal_id": "alice",
        "descriptor_id": descriptor["id"], "descriptor_generation": 1, "projection_epoch": 1,
        "canonical_ref": target, "canonical_old": old, "canonical_new": new,
        "projected_ref": target, "projected_old": old, "projected_new": submitted,
        "canonical_frontier_old": old, "canonical_pack": canonical_pack,
        "submission_pack": submitted_pack,
        "mappings": [{"submitted": submitted, "canonical": new, "change_id": None}],
        "received_at": 2, "transport": "ssh", "remote_identity": None,
        "source_ref": None, "receive_old": old}
    repo.publish(lambda manifest, state: Candidate(
        record_fingerprint(repo.repository_id, "branch_submission", payload), payload))


def test_real_repository_publishes_and_cold_replays_creation_with_per_view_frontiers():
    repo, descriptor = fixture()
    before = repo.load()[2]
    assert pure_fork(repo, descriptor) == 3
    state = repo.load()[2]
    assert state.canonical_refs["refs/heads/topic"] == TIP
    assert state.canonical_tip == TIP
    assert state.active_views["v1"]["branch_frontiers"]["refs/heads/topic"] == TIP
    assert "refs/heads/topic" not in before.active_views["v1"]["branch_frontiers"]
    cold = repo.load(use_cache=False)[2]
    assert cold.canonical_refs == state.canonical_refs


def test_policy_transition_pins_current_all_branch_tips_and_keeps_prior_snapshot_immutable():
    repo, descriptor = fixture()
    pure_fork(repo, descriptor)
    advance_ref(repo, descriptor, "refs/heads/topic", TIP, "3" * 40, "4" * 40)
    advance_ref(repo, descriptor, "refs/heads/main", TIP, "5" * 40, "6" * 40)
    old = repo.load()[2]
    replacement = v4_descriptor(empty_permissions())
    repo.put_descriptor(replacement)
    transition = {"principal_id": "alice", "canonical_frontier": "5" * 40,
        "views": [{"view_id": "v1", "old_descriptor_id": descriptor["id"],
            "old_generation": 1, "old_projection_epoch": 1,
            "new_descriptor_id": replacement["id"], "new_generation": 2,
            "new_projection_epoch": 2}]}
    repo.publish(lambda manifest, state: Candidate(
        record_fingerprint(repo.repository_id, "policy_transition", transition), transition))
    current = repo.load()[2].active_views["v1"]
    expected = {"refs/heads/main": "5" * 40, "refs/heads/topic": "3" * 40}
    assert dict(current["branch_frontiers"]) == expected
    assert dict(current["epoch_branches"]) == expected
    assert dict(old.active_views["v1"]["branch_frontiers"]) == {
        "refs/heads/main": "5" * 40, "refs/heads/topic": "3" * 40}
    assert dict(old.active_views["v1"]["epoch_branches"]) == {
        "refs/heads/main": TIP}
    cold = repo.load(use_cache=False)[2]
    assert dict(cold.active_views["v1"]["epoch_branches"]) == expected


@pytest.mark.parametrize("changes", [
    {"receive_old": TIP},
    {"canonical_frontier_old": "3" * 40},
    {"canonical_ref": "refs/heads/bad..name", "projected_ref": "refs/heads/bad..name"},
    {"projected_ref": "refs/heads/not-topic"},
    {"source_ref": "refs/heads/missing"},
])
def test_malformed_or_stale_creation_is_rejected_without_publishing(changes):
    repo, descriptor = fixture()
    with pytest.raises(ForgeError):
        pure_fork(repo, descriptor, **changes)
    assert "refs/heads/topic" not in repo.load()[2].canonical_refs


def test_namespace_conflicts_and_v3_descriptors_cannot_transport_create():
    repo, descriptor = fixture()
    pure_fork(repo, descriptor, target="refs/heads/topic/child")
    with pytest.raises(ForgeError):
        pure_fork(repo, descriptor, target="refs/heads/topic")
