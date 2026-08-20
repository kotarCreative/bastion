"""Shared-visible conflict tests for overlapping permission views.

These tests exercise the roadmap item:

> Add shared-visible conflict tests: modify/modify, delete/modify, and mode/content.

Each scenario constructs two views that can both read and write a shared path,
then submits conflicting changes from each view.  Because source pushes are
serialized, one change is accepted first; the other is rejected as stale, must
fetch/rebase, and then succeeds.  The tests verify that canonical history
contains both effects in the serialized order and that the losing view leaves no
authoritative trace before it rebases.
"""

from __future__ import annotations

import os
import unittest

import forge
from tests.tree_harness import TreeFixture, command, git, oid, tree_at


class SharedConflictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def _push_and_rebase(
        self,
        first_view: str,
        first_effects: dict[bytes, tuple[bytes, bytes] | None],
        second_view: str,
        second_effects: dict[bytes, tuple[bytes, bytes] | None],
        rebased_effects: dict[bytes, tuple[bytes, bytes] | None],
    ) -> tuple[str, str, str]:
        """Build both commits, push first, reject second, rebase second, then push.

        Returns (first_submitted, rejected_new, rebased_new).
        """
        _first_old, first_new = self.fixture.push_source(first_view, first_effects)
        _second_old, second_new = self.fixture.source_commit(second_view, second_effects, b"conflicting shared change\n")

        second_client = self.fixture.clients[second_view]
        before = self.fixture.rejection_snapshot()
        result = command("git", "-C", os.fsdecode(second_client), "push", "origin", "main", check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn(b"fetch first", result.stderr)
        self.fixture.assert_rejection(before)

        command("git", "-C", os.fsdecode(second_client), "fetch", "-q", "origin")
        git(second_client, "update-ref", "refs/heads/main", oid(second_client, "refs/remotes/origin/main"))
        _rebased_old, rebased_new = self.fixture.push_source(second_view, rebased_effects)
        return first_new, second_new, rebased_new

    def test_modify_modify_conflict_serializes_through_rebase(self) -> None:
        """Two views modify the same shared file; loser rebases and both effects land."""
        alice_new, _bob_loser, bob_rebased = self._push_and_rebase(
            "alice",
            {b"shared/base.txt": (b"100644", b"alice version\n")},
            "bob",
            {b"shared/base.txt": (b"100644", b"bob version\n")},
            {b"shared/base.txt": (b"100644", b"bob version\n")},
        )

        canonical = forge.canonical_repo(self.fixture.root)
        canonical_tip = oid(canonical, "refs/heads/main")
        commits = git(canonical, "rev-list", "--reverse", "--first-parent", "refs/heads/main").stdout.decode("ascii").splitlines()
        self.assertEqual(3, len(commits))
        self.assertEqual(bob_rebased, oid(forge.view_repo(self.fixture.root, "bob"), "refs/heads/main"))
        alice_history = git(forge.view_repo(self.fixture.root, "alice"), "rev-list", "--reverse", "--first-parent", "refs/heads/main").stdout.decode("ascii").splitlines()
        self.assertIn(alice_new, alice_history)
        self.assertEqual(b"alice version\n", git(canonical, "cat-file", "blob", f"{commits[1]}:shared/base.txt").stdout)
        self.assertEqual(b"bob version\n", git(canonical, "cat-file", "blob", f"{commits[2]}:shared/base.txt").stdout)
        self.assertEqual(b"bob version\n", git(canonical, "cat-file", "blob", f"{canonical_tip}:shared/base.txt").stdout)

    def test_delete_modify_conflict_serializes_through_rebase(self) -> None:
        """One view deletes a shared file while another modifies it; loser recreates."""
        alice_new, _bob_loser, bob_rebased = self._push_and_rebase(
            "alice",
            {b"shared/delete.txt": None},
            "bob",
            {b"shared/delete.txt": (b"100644", b"bob modified\n")},
            {b"shared/delete.txt": (b"100644", b"bob recreated\n")},
        )

        canonical = forge.canonical_repo(self.fixture.root)
        canonical_tip = oid(canonical, "refs/heads/main")
        commits = git(canonical, "rev-list", "--reverse", "--first-parent", "refs/heads/main").stdout.decode("ascii").splitlines()
        self.assertEqual(3, len(commits))
        self.assertEqual(bob_rebased, oid(forge.view_repo(self.fixture.root, "bob"), "refs/heads/main"))
        alice_history = git(forge.view_repo(self.fixture.root, "alice"), "rev-list", "--reverse", "--first-parent", "refs/heads/main").stdout.decode("ascii").splitlines()
        self.assertIn(alice_new, alice_history)
        self.assertNotIn(b"shared/delete.txt", tree_at(canonical, commits[1]))
        self.assertIn(b"shared/delete.txt", tree_at(canonical, commits[2]))
        self.assertEqual(b"bob recreated\n", git(canonical, "cat-file", "blob", f"{canonical_tip}:shared/delete.txt").stdout)

    def test_mode_content_conflict_preserves_both_effects(self) -> None:
        """One view changes mode while another changes content; rebase keeps both."""
        alice_new, _bob_loser, bob_rebased = self._push_and_rebase(
            "alice",
            {b"shared/mode-up": (b"100755", b"mode up\n")},
            "bob",
            {b"shared/mode-up": (b"100644", b"bob content\n")},
            # After rebase, Bob's content applies on top of Alice's mode change.
            {b"shared/mode-up": (b"100755", b"bob content\n")},
        )

        canonical = forge.canonical_repo(self.fixture.root)
        canonical_tip = oid(canonical, "refs/heads/main")
        commits = git(canonical, "rev-list", "--reverse", "--first-parent", "refs/heads/main").stdout.decode("ascii").splitlines()
        self.assertEqual(3, len(commits))
        self.assertEqual(bob_rebased, oid(forge.view_repo(self.fixture.root, "bob"), "refs/heads/main"))
        alice_history = git(forge.view_repo(self.fixture.root, "alice"), "rev-list", "--reverse", "--first-parent", "refs/heads/main").stdout.decode("ascii").splitlines()
        self.assertIn(alice_new, alice_history)
        self.assertEqual(b"100755", tree_at(canonical, commits[1])[b"shared/mode-up"][0])
        self.assertEqual(b"mode up\n", tree_at(canonical, commits[1])[b"shared/mode-up"][2])
        self.assertEqual(b"100755", tree_at(canonical, commits[2])[b"shared/mode-up"][0])
        self.assertEqual(b"bob content\n", tree_at(canonical, commits[2])[b"shared/mode-up"][2])
        self.assertEqual(b"100755", tree_at(canonical, canonical_tip)[b"shared/mode-up"][0])
        self.assertEqual(b"bob content\n", tree_at(canonical, canonical_tip)[b"shared/mode-up"][2])

    def test_loser_change_id_and_path_absent_before_rebase(self) -> None:
        """The rejected shared-visible commit leaves no canonical mapping or representation."""
        _, bob_loser, _bob_rebased = self._push_and_rebase(
            "alice",
            {b"shared/base.txt": (b"100644", b"alice version\n")},
            "bob",
            {b"shared/base.txt": (b"100644", b"bob loser\n")},
            {b"shared/base.txt": (b"100644", b"bob winner\n")},
        )

        state = self.fixture.state()
        canonical = forge.canonical_repo(self.fixture.root)
        for tx in state["transactions"].values():
            if tx.get("view") == "bob":
                for mapping in tx["mappings"]:
                    self.assertNotEqual(bob_loser, mapping["submitted"])
                    self.assertNotEqual(b"bob loser\n", git(canonical, "cat-file", "blob", f"{mapping['canonical']}:shared/base.txt").stdout)
        for change in state["changes"].values():
            for revision in change["revisions"]:
                if revision["view"] == "bob" and revision["kind"] == "submitted":
                    self.assertNotEqual(bob_loser, revision["submitted"])


if __name__ == "__main__":
    unittest.main()
