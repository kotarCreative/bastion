"""Spec 015 A2: view creation fenced against policy transitions.

Deterministic barriers cover creation before/after revocation, creation
losing CAS to a transition, and multiple creations for one principal.
After both operations finish, no newly committed view exposes the revoked
path.  Dormant catalog semantics are preserved: adding a catalog role
alone does not silently change the committed assignment.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import identity
from git import clean_env
from repository import Repository
from service import add_role, add_view, bind_role, transition_principal
from wal import event_id, record_fingerprint
from tests.storage_helpers import repository_state
from tests.tree_harness import TreeFixture
import forge
import wal


def empty_capabilities() -> dict[str, list[str]]:
    return {capability: [] for capability in identity.CAPABILITIES}


def bind_read_restriction(root: Path, role_id: str, principal: str, path: str) -> None:
    """Bind a dormant catalog role whose read grant is restricted to path.

    Roles require at least one allow grant, so the role allows create
    everywhere while restricting read on the revoked path; the effective
    read coverage of the principal loses the path either way.
    """
    allow = empty_capabilities()
    allow["create"] = [""]
    deny = empty_capabilities()
    deny["read"] = [path]
    add_role(root, role_id, allow, deny)
    bind_role(root, role_id, "principal", principal)


def revoke_team_a(root: Path) -> None:
    """Commit the catalog change and the transition that revokes team-a."""
    bind_read_restriction(root, "revoke-team-a", "alice", "team-a")
    transition_principal(root, "alice")


def view_blob_present(root: Path, view: str, blob: str) -> bool:
    import subprocess

    result = subprocess.run(
        [
            "git",
            "--git-dir",
            os.fsdecode(forge.view_repo(root, view)),
            "cat-file",
            "-e",
            blob,
        ],
        capture_output=True,
        env=clean_env(),
    )
    return result.returncode == 0


class PolicyFencingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)
        self.root = self.fixture.root
        self.team_a_blob = self.fixture.canonical_model[b"team-a/private.txt"][1]
        manifest, replay = repository_state(self.root)
        self.d0 = replay.active_views["alice"]["descriptor_id"]

    def view_record(self, name: str) -> dict:
        _manifest, replay = repository_state(self.root)
        return replay.active_views[name]

    def assert_revoked(self, name: str) -> None:
        view = self.view_record(name)
        self.assertNotEqual(view["descriptor_id"], self.d0)
        self.assertFalse(
            view_blob_present(self.root, name, self.team_a_blob),
            f"{name} exposes the revoked path",
        )

    def gated_publish(self, interleave) -> None:
        """Run add_view with a full interleave inside its first CAS attempt."""
        original = Repository.publish
        state = {"pending": True}

        def patched(self_repository, build):
            def gated_build(manifest, replay):
                candidate = build(manifest, replay)
                if state["pending"]:
                    state["pending"] = False
                    interleave()
                return candidate

            return original(self_repository, gated_build)

        with patch.object(Repository, "publish", patched):
            return add_view(self.root, self.name, "alice", "main")

    def test_creation_losing_cas_to_revocation_is_fenced(self) -> None:
        self.name = "alice-extra"
        manifest_before, _replay = repository_state(self.root)
        self.gated_publish(lambda: revoke_team_a(self.root))

        _manifest, replay = repository_state(self.root)
        creation = next(
            index
            for index, record in enumerate(replay.records)
            if record["kind"] == "view_create" and record["payload"]["view_id"] == self.name
        )
        transition = max(
            index for index, record in enumerate(replay.records)
            if record["kind"] == "policy_transition"
        )
        self.assertLess(
            transition,
            creation,
            "transition must be committed before the fenced creation",
        )
        self.assert_revoked(self.name)
        self.assert_revoked("alice")
        self.assertEqual(
            self.view_record(self.name)["descriptor_id"],
            self.view_record("alice")["descriptor_id"],
        )
        forge.check_invariants(self.root)
        self.assertEqual(manifest_before.repository_id, repository_state(self.root)[0].repository_id)

    def test_creation_before_revocation(self) -> None:
        add_view(self.root, "alice-extra", "alice", "main")
        self.assertEqual(
            self.view_record("alice-extra")["descriptor_id"], self.d0
        )
        self.assertTrue(view_blob_present(self.root, "alice-extra", self.team_a_blob))
        revoke_team_a(self.root)
        self.assert_revoked("alice-extra")
        self.assert_revoked("alice")
        forge.check_invariants(self.root)

    def test_multiple_creations_for_one_principal(self) -> None:
        self.name = "alice-x"
        self.gated_publish(lambda: add_view(self.root, "alice-y", "alice", "main"))
        x = self.view_record("alice-x")
        y = self.view_record("alice-y")
        self.assertEqual(x["descriptor_id"], y["descriptor_id"])
        self.assertEqual(x["descriptor_id"], self.d0)
        forge.check_invariants(self.root)

    def test_multiple_creations_with_intervening_revocation(self) -> None:
        self.name = "alice-x"
        self.gated_publish(
            lambda: (
                revoke_team_a(self.root),
                add_view(self.root, "alice-y", "alice", "main"),
            )
        )
        for name in ("alice", "alice-x", "alice-y"):
            self.assert_revoked(name)
            self.assertEqual(
                self.view_record(name)["descriptor_id"],
                self.view_record("alice")["descriptor_id"],
            )
        forge.check_invariants(self.root)

    def test_dormant_catalog_change_keeps_committed_assignment(self) -> None:
        bind_read_restriction(self.root, "revoke-team-a", "alice", "team-a")
        add_view(self.root, "alice-extra", "alice", "main")
        view = self.view_record("alice-extra")
        self.assertEqual(view["descriptor_id"], self.d0)
        self.assertTrue(
            view_blob_present(self.root, "alice-extra", self.team_a_blob),
            "dormant catalog additions must not silently change the assignment",
        )
        forge.check_invariants(self.root)

    def test_wal_replay_rejects_mismatched_assignment(self) -> None:
        manifest, replay = repository_state(self.root)
        bob_descriptor = replay.active_views["bob"]["descriptor_id"]
        head = manifest.wal_head
        payload = {
            "view_id": "alice-forge",
            "principal_id": "alice",
            "projected_ref": "refs/heads/main",
            "descriptor_id": bob_descriptor,
            "descriptor_generation": 1,
            "projection_epoch": 1,
            "canonical_frontier": manifest.canonical_tip,
        }
        fingerprint = record_fingerprint(manifest.repository_id, "view_create", payload)
        record = {
            "seq": manifest.head_seq + 1,
            "kind": "view_create",
            "event_fingerprint": fingerprint,
            "event_id": event_id(fingerprint),
            "publication_id": "0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f",
            "previous": {
                "seq": head.seq,
                "key": head.key,
                "sha256": head.sha256,
            },
            "created_at": 0,
            "writer": "0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e0e",
            "payload": payload,
        }
        candidate = replay.clone_for_append()
        with self.assertRaises(wal.ForgeError) as caught:
            wal.apply_record(candidate, record)
        self.assertIn("committed assignment", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
