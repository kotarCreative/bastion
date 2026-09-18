"""Account credentials and owner-managed path privacy integration tests."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import forge
from accounts import set_password, verify_password
from identity import CAPABILITIES
from errors import ForgeError
from repository import Repository
from store import backup_forge, restore_forge
from service import (
    add_role,
    bind_role,
    managed_private_paths,
    set_path_privacy,
    transition_principal,
)
from state import load_state
from tests.tree_harness import TreeFixture, tree_at


class AccountAndPrivacyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def _make_alice_owner(self) -> None:
        allow = {capability: [] for capability in CAPABILITIES}
        deny = {capability: [] for capability in CAPABILITIES}
        allow["admin"] = [""]
        add_role(self.fixture.root, "alice-owner", allow, deny)
        bind_role(self.fixture.root, "alice-owner", "principal", "alice")
        transition_principal(self.fixture.root, "alice")

    def test_password_account_is_bound_to_principal_not_descriptor(self) -> None:
        before = load_state(self.fixture.root)["views"]["alice"]["descriptor_id"]
        set_password(self.fixture.root, "alice", "correct horse battery staple")
        self.assertTrue(
            verify_password(self.fixture.root, "alice", "correct horse battery staple")
        )
        self.assertFalse(verify_password(self.fixture.root, "alice", "wrong password"))
        self.assertFalse(verify_password(self.fixture.root, "unknown", "wrong password"))
        self.assertNotIn(
            "correct horse battery staple",
            str(load_state(self.fixture.root)["principals"]["alice"]),
        )
        after = load_state(self.fixture.root)["views"]["alice"]["descriptor_id"]
        self.assertEqual(before, after)

    def test_owner_privacy_edit_reprojects_target_and_can_be_restored(self) -> None:
        self._make_alice_owner()
        # A nested grant would normally override an ancestor deny under v3
        # longest-prefix semantics; UI privacy must still be absolute.
        allow = {capability: [] for capability in CAPABILITIES}
        deny = {capability: [] for capability in CAPABILITIES}
        allow["read"] = ["shared/base.txt"]
        add_role(self.fixture.root, "bob-nested-reader", allow, deny)
        bind_role(self.fixture.root, "bob-nested-reader", "principal", "bob")
        transition_principal(self.fixture.root, "bob")
        paths = set_path_privacy(
            self.fixture.root, "alice", "bob", "shared", True
        )
        self.assertEqual(paths, ["shared"])
        bob_tree = tree_at(forge.view_repo(self.fixture.root, "bob"), "HEAD")
        self.assertNotIn(b"shared/base.txt", bob_tree)
        self.assertEqual(
            managed_private_paths(load_state(self.fixture.root), "bob"),
            ["shared"],
        )
        forge.check_invariants(self.fixture.root)

        paths = set_path_privacy(
            self.fixture.root, "alice", "bob", "shared", False
        )
        self.assertEqual(paths, [])
        bob_tree = tree_at(forge.view_repo(self.fixture.root, "bob"), "HEAD")
        self.assertIn(b"shared/base.txt", bob_tree)
        forge.check_invariants(self.fixture.root)

    def test_rejected_owner_fence_rolls_back_dormant_policy(self) -> None:
        self._make_alice_owner()
        with patch.object(
            Repository,
            "publish",
            side_effect=ForgeError("ownership changed", "conflict"),
        ):
            with self.assertRaisesRegex(ForgeError, "ownership changed"):
                set_path_privacy(
                    self.fixture.root, "alice", "bob", "shared/base.txt", True
                )
        self.assertEqual(managed_private_paths(load_state(self.fixture.root), "bob"), [])

    def test_account_survives_backup_and_restore(self) -> None:
        set_password(self.fixture.root, "alice", "durable account password")
        backup = self.fixture.directory / "account-backup"
        restored = self.fixture.directory / "account-restored"
        backup_forge(self.fixture.root, backup)
        restore_forge(backup, restored)
        self.assertTrue(verify_password(restored, "alice", "durable account password"))
