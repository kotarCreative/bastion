"""Spec 015 C2: session baseline refresh after successful saves.

Repeated insert/update/delete and no-op saves work within one locked
session, recovery-then-write works, and concurrent updates, deletions,
creation collisions, and ABA reverts from other writers still conflict
atomically.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from errors import ForgeError
from state import load_state, locked, save_state
from store import open_store
from tests.tree_harness import TreeFixture, build_commit, command, git, oid
import forge


def stage_direct_receive(fixture: TreeFixture) -> None:
    """Advance the WAL without any locked control-plane session."""
    client = fixture.clients["alice"]
    parent = oid(client, "refs/heads/main")
    new = build_commit(
        client,
        parent,
        {b"shared/baseline-setup.txt": (b"100644", b"setup\n")},
        b"baseline setup\n",
        1_700_000_700,
    )
    from layout import intake_repo

    command(
        "git", "--git-dir", os.fsdecode(intake_repo(fixture.root, "alice")),
        "fetch", "-q", os.fsdecode(client), "refs/heads/main",
    )
    environment = {
        "BASTION_VIEW": "alice",
        "BASTION_PRINCIPAL_ID": "alice",
        "BASTION_TRANSPORT": "test",
    }
    from unittest.mock import patch

    with patch.dict(os.environ, environment, clear=False):
        forge.pre_receive(fixture.root, "alice", f"{parent} {new} refs/heads/main\n".encode())


class SessionBaselineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)
        self.root = self.fixture.root

    def external_save(self, mutate) -> None:
        """A different writer commits one full save without a session."""
        store = open_store(self.root)
        try:
            state = store.load_document()
            mutate(state)
            store.save_document(state, [], self.root)
        finally:
            store.close()

    def test_repeated_insert_update_delete_and_noop_saves(self) -> None:
        with locked(self.root) as state:
            state["principals"]["temp-worker"] = {
                "id": "temp-worker",
                "kind": "service",
            }
            save_state(self.root, state)

            state["principals"]["temp-worker"]["emails"] = ["temp@example.test"]
            save_state(self.root, state)

            state["principals"]["temp-worker"]["kind"] = "user"
            save_state(self.root, state)

            del state["principals"]["temp-worker"]
            save_state(self.root, state)

            # A no-op save inside the same session must succeed untouched.
            save_state(self.root, state)
        persisted = load_state(self.root)
        self.assertNotIn("temp-worker", persisted["principals"])

    def test_repeated_meta_saves_in_one_session(self) -> None:
        with locked(self.root) as state:
            state["last_indexed_seq"] = 1
            save_state(self.root, state)
            state["last_indexed_seq"] = 2
            save_state(self.root, state)
            state["last_indexed_seq"] = 2
            save_state(self.root, state)

    def test_recovery_then_write_in_one_session(self) -> None:
        stage_direct_receive(self.fixture)
        manifest, _replay = None, None
        from tests.storage_helpers import repository_state

        manifest, replay = repository_state(self.root)
        self.assertGreater(manifest.head_seq, 0)
        with locked(self.root) as state:
            # Recovery at session entry rewrote views/meta rows; a
            # subsequent write in the same session must not conflict with
            # the session's own recovery save.
            state["principals"]["alice"]["emails"] = ["recovered@example.test"]
            save_state(self.root, state)
            state["principals"]["alice"]["emails"] = ["recovered2@example.test"]
            save_state(self.root, state)
        persisted = load_state(self.root)
        self.assertEqual(
            persisted["principals"]["alice"]["emails"], ["recovered2@example.test"]
        )
        self.assertEqual(persisted["last_indexed_seq"], manifest.head_seq)

    def test_external_update_still_conflicts_atomically(self) -> None:
        with locked(self.root) as state:
            self.external_save(
                lambda other: other["principals"]["alice"].__setitem__(
                    "emails", ["external@example.test"]
                )
            )
            state["principals"]["alice"]["emails"] = ["session@example.test"]
            with self.assertRaises(ForgeError) as caught:
                save_state(self.root, state)
            self.assertIn("optimistic concurrency", str(caught.exception))
        persisted = load_state(self.root)
        self.assertEqual(
            persisted["principals"]["alice"]["emails"], ["external@example.test"]
        )

    def test_external_creation_collision_still_conflicts(self) -> None:
        with locked(self.root) as state:
            self.external_save(
                lambda other: other["principals"].__setitem__(
                    "racer", {"id": "racer", "kind": "user"}
                )
            )
            state["principals"]["racer"] = {"id": "racer", "kind": "service"}
            with self.assertRaises(ForgeError):
                save_state(self.root, state)
        persisted = load_state(self.root)
        self.assertEqual(persisted["principals"]["racer"]["kind"], "user")

    def test_external_delete_conflicts_with_stale_update(self) -> None:
        def add(other: dict) -> None:
            other["principals"]["doomed"] = {"id": "doomed", "kind": "user"}

        self.external_save(add)
        with locked(self.root) as state:
            row = state["principals"].pop("doomed")
            def resurrect(other: dict) -> None:
                other["principals"]["doomed"] = {
                    **row, "kind": "service",
                }
            # Commit an external update after this session's baseline was
            # taken: the stale update must conflict, not overwrite.
            self.external_save(resurrect)
            with self.assertRaises(ForgeError):
                save_state(self.root, state)
        persisted = load_state(self.root)
        self.assertEqual(persisted["principals"]["doomed"]["kind"], "service")

    def test_aba_revert_still_conflicts(self) -> None:
        # The revert happens after this session loaded its baseline, so the
        # content matches the baseline again but the version moved: an ABA
        # change another writer committed must conflict.
        with locked(self.root) as state:
            self.external_save(
                lambda other: other["principals"]["alice"].__setitem__(
                    "emails", ["swap@example.test"]
                )
            )
            self.external_save(
                lambda other: other["principals"]["alice"].pop("emails", None)
            )
            state["principals"]["alice"]["emails"] = ["session@example.test"]
            with self.assertRaises(ForgeError):
                save_state(self.root, state)
        persisted = load_state(self.root)
        self.assertNotIn("emails", persisted["principals"]["alice"])

    def test_failed_save_leaves_last_committed_baseline(self) -> None:
        with locked(self.root) as state:
            state["principals"]["keeper"] = {"id": "keeper", "kind": "user"}
            save_state(self.root, state)
            def mutate(other: dict) -> None:
                other["principals"]["keeper"]["kind"] = "service"

            self.external_save(mutate)
            state["principals"]["keeper"]["emails"] = ["late@example.test"]
            with self.assertRaises(ForgeError):
                save_state(self.root, state)
            # Reverting the rejected mutation and saving again works: the
            # baseline reflects the last committed save, not the failure.
            state["principals"]["keeper"].pop("emails")
            save_state(self.root, state)
            state["principals"]["later"] = {"id": "later", "kind": "user"}
            save_state(self.root, state)
        persisted = load_state(self.root)
        self.assertEqual(persisted["principals"]["keeper"]["kind"], "service")
        self.assertEqual(persisted["principals"]["later"]["kind"], "user")


if __name__ == "__main__":
    unittest.main()
