"""Idempotency-key semantics on the receive path."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from errors import ForgeError
from protocol import post_receive, pre_receive
from state import load_state
from store import open_store
from tests.tree_harness import TreeFixture, build_commit, command, oid
from worker import drain_projection_jobs


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)
        self.view_repo = self.fixture.root / "views" / "alice.git"

    def _updates(self, effects: dict, message: bytes = b"change\n") -> bytes:
        """Build a commit in the client clone and return hook stdin bytes."""
        client = self.fixture.clients["alice"]
        old = oid(client, "refs/heads/main")
        new = build_commit(client, old, effects, message, 1_700_000_500)
        # Quarantine-equivalent: objects present in the view repository
        # without moving its ref, exactly what receive-pack provides.
        command(
            "git",
            "-C",
            self.view_repo,
            "fetch",
            "-q",
            str(client),
            new,
        )
        return f"{old} {new} refs/heads/main\n".encode()

    def _receive(self, view: str, updates: bytes, key: str | None) -> bool:
        """Run the receive contract; returns True when newly accepted."""
        environment = {"BASTION_IDEMPOTENCY_KEY": key} if key else {}
        before = sorted(load_state(self.fixture.root)["transactions"])
        import os

        with patch_dict(environment):
            pre_receive(self.fixture.root, view, updates)
        after = sorted(load_state(self.fixture.root)["transactions"])
        if after == before:
            return False
        # Pre-receive publishes the source publication itself under the
        # intake boundary; only post-receive confirmation follows.
        with patch_dict(environment):
            post_receive(self.fixture.root, view, updates)
        drain_projection_jobs(self.fixture.root)
        return True


class patch_dict:
    def __init__(self, environment: dict):
        self.environment = environment

    def __enter__(self):
        import os
        from unittest.mock import patch

        self._patcher = patch.dict(os.environ, self.environment, clear=False)
        self._patcher.start()
        return self

    def __exit__(self, *_args):
        self._patcher.stop()


class AcceptedFlowTest(IdempotencyTest):
    def test_first_apply_then_replay_is_a_noop(self) -> None:
        updates = self._updates({b"shared/idem.txt": (b"100644", b"one\n")})
        self.assertTrue(self._receive("alice", updates, "key-1"))

        state_before = load_state(self.fixture.root)
        transactions_before = sorted(state_before["transactions"])
        canonical_tip = command(
            "git", "-C", self.fixture.root / "canonical.git", "rev-parse", "refs/heads/main"
        ).stdout.strip()

        # Replay of the identical request under the same key: recorded
        # outcome is returned and nothing is re-applied.
        self.assertFalse(self._receive("alice", updates, "key-1"))

        state_after = load_state(self.fixture.root)
        self.assertEqual(transactions_before, sorted(state_after["transactions"]))
        self.assertEqual(
            canonical_tip,
            command(
                "git",
                "-C",
                self.fixture.root / "canonical.git",
                "rev-parse",
                "refs/heads/main",
            ).stdout.strip(),
        )
        self.assertEqual(
            state_before["views"]["alice"]["projected_tip"],
            state_after["views"]["alice"]["projected_tip"],
        )

    def test_recorded_outcome_persists_across_store_reopen(self) -> None:
        updates = self._updates({b"shared/restart.txt": (b"100644", b"restart\n")})
        self.assertTrue(self._receive("alice", updates, "persisted"))
        store = open_store(self.fixture.root)
        record = store.lookup_idempotency("persisted")
        store.close()
        self.assertIsNotNone(record)
        self.assertEqual("prepared", record["outcome"]["status"])

    def test_missing_key_behaves_normally(self) -> None:
        updates = self._updates({b"shared/plain.txt": (b"100644", b"plain\n")})
        self.assertTrue(self._receive("alice", updates, None))
        state = load_state(self.fixture.root)
        self.assertEqual(1, len(state["transactions"]))


class RejectionTest(IdempotencyTest):
    def test_same_key_with_different_request_is_rejected(self) -> None:
        first = self._updates({b"shared/idem-a.txt": (b"100644", b"a\n")})
        self.assertTrue(self._receive("alice", first, "shared-key"))

        second = self._updates({b"shared/idem-b.txt": (b"100644", b"b\n")})
        with self.assertRaises(ForgeError):
            self._receive("alice", second, "shared-key")

    def test_fingerprint_mismatch_names_the_conflict(self) -> None:
        first = self._updates({b"shared/idem-c.txt": (b"100644", b"c\n")})
        self.assertTrue(self._receive("alice", first, "conflict-key"))
        second = self._updates({b"shared/idem-d.txt": (b"100644", b"d\n")})
        with self.assertRaises(ForgeError) as caught:
            self._receive("alice", second, "conflict-key")
        self.assertIn("different request", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
