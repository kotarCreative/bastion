from __future__ import annotations

import os
import unittest

import forge
from tests import identity_harness
from errors import ForgeError
from tests.tree_harness import TreeFixture, command, empty_subtree_commit, git, gitlink_commit, oid
from tests.transport import attach_intake_push


class TreeOperationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def _source_transaction(self, view: str, old: str, new: str) -> dict:
        return next(
            tx for tx in self.fixture.state()["transactions"].values()
            if tx.get("view") == view and tx.get("projected_old") == old and tx.get("projected_new") == new
        )

    def _reject_source(self, view: str, effects: dict[bytes, tuple[bytes, bytes] | None], category: str) -> None:
        client = self.fixture.clients[view]
        command("git", "-C", os.fsdecode(client), "fetch", "-q", "origin")
        git(client, "update-ref", "refs/heads/main", oid(client, "refs/remotes/origin/main"))
        _old, _new = self.fixture.source_commit(view, effects, b"rejected source change\n")
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(self.fixture.clients[view]), "push", "origin", "main", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(category.encode(), result.stderr)
        self.fixture.assert_rejection(before)

    def test_shared_private_delete_rename_and_mode_source_semantics(self) -> None:
        old, new = self.fixture.push_source("alice", {b"shared/delete.txt": None})
        transaction = self._source_transaction("alice", old, new)
        self.assertEqual(new, oid(forge.view_repo(self.fixture.root, "alice"), "refs/heads/main"))
        self.assertEqual("submitted", self.fixture.state()["views"]["alice"]["anchors"][new]["kind"])
        self.assertEqual(1, len(transaction["mappings"]))

        bob_tip = self.fixture.state()["views"]["bob"]["projected_tip"]
        self.fixture.push_source("alice", {b"team-a/private-delete.txt": None})
        bob = self.fixture.state()["views"]["bob"]
        self.assertEqual(bob_tip, bob["projected_tip"])
        self.assertEqual(oid(forge.canonical_repo(self.fixture.root), "refs/heads/main"), bob["canonical_frontier"])

        self.fixture.push_source("alice", {
            b"shared/rename.txt": None,
            b"shared/renamed.txt": (b"100644", b"shared rename\n"),
            b"team-a/private-exec": None,
            b"shared/from-private": (b"100755", b"private executable\n"),
            b"shared/link": None,
            b"team-a/from-shared-link": (b"120000", b"base.txt"),
        })
        self.assertNotIn(b"shared/rename.txt", self.fixture.canonical_model)
        self.assertEqual(b"100755", self.fixture.canonical_model[b"shared/from-private"][0])
        self.assertEqual(b"120000", self.fixture.canonical_model[b"team-a/from-shared-link"][0])

        before_up = self.fixture.canonical_model[b"shared/mode-up"][1]
        before_down = self.fixture.canonical_model[b"shared/mode-down"][1]
        self.fixture.push_source("alice", {
            b"shared/mode-up": (b"100755", b"mode up\n"),
            b"shared/mode-down": (b"100644", b"mode down\n"),
        })
        self.assertEqual(before_up, self.fixture.canonical_model[b"shared/mode-up"][1])
        self.assertEqual(before_down, self.fixture.canonical_model[b"shared/mode-down"][1])
        self.assertEqual(b"100755", self.fixture.canonical_model[b"shared/mode-up"][0])
        self.assertEqual(b"100644", self.fixture.canonical_model[b"shared/mode-down"][0])
        bob_tip = self.fixture.state()["views"]["bob"]["projected_tip"]
        self.fixture.push_source("alice", {b"team-a/private.txt": (b"100755", b"alice private\n")})
        self.assertEqual(bob_tip, self.fixture.state()["views"]["bob"]["projected_tip"])

    def test_symlink_and_file_directory_source_lifecycle(self) -> None:
        lifecycle = (
            {b"shared/new-link": (b"120000", b"../../hidden/secret.txt")},
            {b"shared/new-link": (b"120000", b"/not/a/server/read")},
            {b"shared/new-link": (b"100644", b"/not/a/server/read")},
            {b"shared/new-link": (b"120000", b"/not/a/server/read")},
        )
        for effects in lifecycle:
            self.fixture.push_source("alice", effects)
        link_oid = self.fixture.canonical_model[b"shared/new-link"][1]
        self.fixture.push_source("alice", {
            b"shared/new-link": None,
            b"shared/renamed-link": (b"120000", b"/not/a/server/read"),
        })
        self.assertEqual(link_oid, self.fixture.canonical_model[b"shared/renamed-link"][1])
        self.fixture.push_source("alice", {b"shared/renamed-link": None})

        bob_tip = self.fixture.state()["views"]["bob"]["projected_tip"]
        self.fixture.push_source("alice", {b"team-a/private-link": (b"120000", b"../../hidden/secret.txt")})
        self.assertEqual(bob_tip, self.fixture.state()["views"]["bob"]["projected_tip"])

        self.fixture.push_source("alice", {
            b"shared/node": None,
            b"shared/node/one": (b"100644", b"one\n"),
            b"shared/node/two": (b"100644", b"two\n"),
        })
        self.fixture.push_source("alice", {
            b"shared/node/one": None,
            b"shared/node/two": None,
            b"shared/node": (b"100755", b"node executable\n"),
        })
        self.assertEqual(b"100755", self.fixture.canonical_model[b"shared/node"][0])
        self.assertNotIn(b"shared/node/one", self.fixture.canonical_model)

    def test_focused_source_rejections_and_hidden_ancestor(self) -> None:
        self._reject_source("limited", {b"team-a/private-delete.txt": None}, "outside current authorization")
        self._reject_source("limited", {
            b"team-a/private.txt": None,
            b"shared/private-moved": (b"100644", b"alice private\n"),
        }, "outside current authorization")
        self._reject_source("limited", {b"team-a/private.txt": (b"100755", b"alice private\n")}, "outside current authorization")
        self._reject_source("limited", {b"team-a/link": (b"120000", b"../secret")}, "outside current authorization")

        self.fixture.admin_update([{b"container": (b"100644", b"hidden container\n")}])
        identity_harness.add_view(self.fixture.root, "narrow", ["container/visible"], ["container/visible"], "main")
        checkout = self.fixture.directory / "client-narrow"
        command("git", "clone", "-q", "--no-local", os.fsdecode(forge.view_repo(self.fixture.root, "narrow")), os.fsdecode(checkout))
        attach_intake_push(self.fixture.root, checkout, "narrow")
        self.fixture.policies["narrow"] = (["container/visible"], ["container/visible"])
        self.fixture.clients["narrow"] = checkout
        self._reject_source("narrow", {b"container/visible/child": (b"100644", b"child\n")}, "file/directory authorization boundary")

    def test_source_empty_subtree_and_gitlink_reject_without_movement(self) -> None:
        client = self.fixture.clients["alice"]
        parent = oid(client, "refs/heads/main")
        empty_subtree_commit(client, parent, 1_700_000_100)
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(client), "push", "origin", "main", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"canonical round-trip does not reproduce the submitted tree", result.stderr)
        self.fixture.assert_rejection(before)

        git(client, "update-ref", "refs/heads/main", parent)
        gitlink_commit(client, parent, 1_700_000_101)
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(client), "push", "origin", "main", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"unsupported entry type or mode", result.stderr)
        self.fixture.assert_rejection(before)

    def test_administrative_operation_chain_and_rejections(self) -> None:
        commits = self.fixture.admin_update([
            {b"shared/delete.txt": None},
            {b"shared/rename.txt": None, b"shared/renamed.txt": (b"100644", b"shared rename\n")},
            {b"shared/mode-up": (b"100755", b"mode up\n")},
            {b"shared/new-link": (b"120000", b"../../hidden/secret.txt")},
            {b"shared/node": None, b"shared/node/one": (b"100644", b"one\n")},
            {b"shared/node/one": None, b"shared/node": (b"100755", b"node executable\n")},
        ])
        transaction = next(tx for tx in self.fixture.state()["transactions"].values() if tx.get("kind") == "canonical_update" and tx["canonical_new"] == commits[-1])
        self.assertNotIn("mappings", transaction)
        self.assertEqual(6, len(commits))
        bob_tip = self.fixture.state()["views"]["bob"]["projected_tip"]
        self.fixture.admin_update([{b"team-a/private.txt": (b"100755", b"admin private\n")}])
        self.assertEqual(bob_tip, self.fixture.state()["views"]["bob"]["projected_tip"])

        canonical = forge.canonical_repo(self.fixture.root)
        expected = oid(canonical, "refs/heads/main")
        git(self.fixture.admin, "update-ref", "refs/heads/main", expected)
        bad = gitlink_commit(self.fixture.admin, expected, 1_700_000_200)
        command("git", "-C", os.fsdecode(self.fixture.admin), "push", "origin", f"{bad}:refs/staging/bad-gitlink")
        before = self.fixture.rejection_snapshot()
        with self.assertRaisesRegex(ForgeError, "unsupported tree entry"):
            identity_harness.update_canonical(self.fixture.root, expected, bad)
        self.fixture.assert_rejection(before)

        git(self.fixture.admin, "update-ref", "refs/heads/main", expected)
        bad = empty_subtree_commit(self.fixture.admin, expected, 1_700_000_201)
        command("git", "-C", os.fsdecode(self.fixture.admin), "push", "origin", f"{bad}:refs/staging/bad-empty")
        before = self.fixture.rejection_snapshot()
        with self.assertRaisesRegex(ForgeError, "explicit empty subtrees are not supported"):
            identity_harness.update_canonical(self.fixture.root, expected, bad)
        self.fixture.assert_rejection(before)


if __name__ == "__main__":
    unittest.main()
