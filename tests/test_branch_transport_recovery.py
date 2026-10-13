"""Regression coverage for branch-transport recovery edge cases."""

import subprocess
import json
from types import SimpleNamespace

import forge
import identity
from materialization import ensure_canonical_materialized, ensure_view_materialized, initialize_view_generation
from invariants import _validate_packs
from permissions import empty_permissions
from repository import Candidate, Repository
from service import add_principal, add_role, add_view, bind_role
from store import backup_forge, restore_forge
from wal import record_fingerprint
from state import load_state
from identity import effective_descriptor
from branch_projection import build_scoped_projection


class _NoPackRepository:
    def get(self, key):  # pragma: no cover - a pure fork has no pack refs
        raise AssertionError(f"unexpected pack lookup: {key}")


def test_pure_branch_fork_has_no_pack_dependencies():
    """Null pack refs on a pure fork must not break backup/checker traversal."""
    replay = SimpleNamespace(
        canonical_packs=[],
        records=[
            {
                "kind": "branch_submission",
                "payload": {"canonical_pack": None, "submission_pack": None},
            }
        ],
    )
    manifest = SimpleNamespace(canonical_packs=[])

    _validate_packs(_NoPackRepository(), replay, manifest)


def test_v4_pure_fork_checker_and_backup_evidence(tmp_path):
    """A real WAL fork survives cold replay, checking, and backup capture."""
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(source)], check=True)
    subprocess.run(["git", "config", "user.name", "Fixture"], cwd=source, check=True)
    subprocess.run(["git", "config", "user.email", "fixture@example.test"], cwd=source, check=True)
    (source / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=source, check=True)
    root = tmp_path / "forge"
    forge.init_forge(root, source, "main")
    add_principal(root, "alice")
    paths = identity.empty_capability_map()
    paths["read"] = [""]
    add_role(root, "alice-path", paths, identity.empty_capability_map())
    bind_role(root, "alice-path", "principal", "alice")
    add_view(root, "alice", "alice", "main")
    policy = empty_permissions()
    for action in ("read", "create", "update"):
        policy["branches"]["allow"][action] = ["refs/heads/*"]
    add_role(root, "alice-branches", identity.empty_capability_map(),
             identity.empty_capability_map(), permissions=policy)
    bind_role(root, "alice-branches", "principal", "alice")
    repository = Repository.open(root)
    state = load_state(root)
    descriptor = effective_descriptor(state, "alice", "refs/heads/main")
    repository.put_descriptor(descriptor)
    _, _, before_transition = repository.load(use_cache=False)
    active_before = before_transition.active_views["alice"]
    transition = {
        "principal_id": "alice", "canonical_frontier": repository.load()[0].canonical_tip,
        "views": [{
            "view_id": "alice", "old_descriptor_id": active_before["descriptor_id"],
            "old_generation": active_before["descriptor_generation"],
            "old_projection_epoch": active_before["projection_epoch"],
            "new_descriptor_id": descriptor["id"],
            "new_generation": active_before["descriptor_generation"] + 1,
            "new_projection_epoch": active_before["projection_epoch"] + 1,
        }],
    }
    repository.publish(lambda _manifest, _state: Candidate(
        record_fingerprint(repository.repository_id, "policy_transition", transition), transition
    ))
    manifest, _, replay = repository.load(use_cache=False)
    active = replay.active_views["alice"]
    canonical = ensure_canonical_materialized(root, manifest, repository)
    projected = tmp_path / "source-projection.git"
    initialize_view_generation(projected, "unborn")
    projection_active = dict(active)
    projection_active["epoch_branches"] = dict(manifest.canonical_refs)
    projection_replay = SimpleNamespace(active_views={"alice": projection_active}, records=replay.records)
    projected_refs = build_scoped_projection(root, "alice", canonical, projected, manifest, projection_replay, repository, descriptor)
    source_ref = "refs/heads/main"
    target_ref = "refs/heads/feature"
    canonical_tip = manifest.canonical_refs[source_ref]
    projected_tip = projected_refs[source_ref]
    payload = {
        "idempotency_key_hash": None, "view_id": "alice", "principal_id": "alice",
        "descriptor_id": descriptor["id"], "descriptor_generation": active["descriptor_generation"],
        "projection_epoch": active["projection_epoch"],
        "canonical_ref": target_ref, "canonical_old": canonical_tip, "canonical_new": canonical_tip,
        "projected_ref": target_ref, "projected_old": projected_tip, "projected_new": projected_tip,
        "canonical_frontier_old": canonical_tip, "canonical_pack": None, "submission_pack": None,
        "mappings": [], "received_at": 1, "transport": "test", "remote_identity": None,
        "source_ref": source_ref, "receive_old": "0" * 40,
    }
    repository.publish(lambda _manifest, _state: Candidate(
        record_fingerprint(repository.repository_id, "branch_submission", payload), payload
    ))
    latest_manifest, _, latest_replay = repository.load(use_cache=False)
    ensure_view_materialized(root, "alice", latest_manifest, latest_replay, repository)

    from invariants import check_invariants
    def disk_snapshot():
        result = {}
        for path in sorted(root.rglob("*")):
            relative = str(path.relative_to(root))
            # SQLite may create transient shared-memory lock files for a
            # read-only WAL connection; these are not catalog/cache writes.
            if relative.endswith(("-shm", "-wal")):
                continue
            if path.is_symlink():
                result[relative] = ("link", path.readlink().as_posix())
            elif path.is_file():
                result[relative] = ("file", path.read_bytes())
        return result

    before_check = disk_snapshot()
    check_invariants(root)
    assert disk_snapshot() == before_check, "checker must not write repository/cache state"
    backup = tmp_path / "backup"
    backup_forge(root, backup)
    cold_manifest, _, cold_replay = repository.load(use_cache=False)
    assert cold_manifest.canonical_refs[target_ref] == canonical_tip
    assert cold_replay.active_views["alice"]["branch_frontiers"][target_ref] == canonical_tip
    backup_objects = {item["key"] for item in json.loads((backup / "manifest.json").read_text())["objects"]}
    assert f"descriptors/{descriptor['id']}.json" in backup_objects
    restored = tmp_path / "restored"
    restore_forge(backup, restored)
    restored_repository = Repository.open(restored)
    restored_manifest, _, restored_replay = restored_repository.load(use_cache=False)
    assert restored_manifest.canonical_refs[target_ref] == canonical_tip
    assert restored_replay.active_views["alice"]["branch_frontiers"][target_ref] == canonical_tip
    check_invariants(restored)
