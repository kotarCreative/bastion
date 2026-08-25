"""Idempotency-key semantics on the receive path."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from errors import ForgeError
from layout import intake_repo
from protocol import pre_receive
from repository import Repository
from tests.tree_harness import TreeFixture, build_commit, command, oid
from wal import idempotency_key_hash
from worker import drain_projection_jobs


class IdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)
        self.intake_repo = intake_repo(self.fixture.root, "alice")

    def _updates(self, effects: dict, message: bytes = b"change\n") -> bytes:
        """Build a commit in the client clone and return hook stdin bytes."""
        client = self.fixture.clients["alice"]
        old = oid(client, "refs/heads/main")
        new = build_commit(client, old, effects, message, 1_700_000_500)
        # Quarantine-equivalent: objects present in the intake repository
        # without moving its ref, exactly what receive-pack provides.
        command(
            "git",
            "--git-dir",
            self.intake_repo,
            "fetch",
            "-q",
            str(client),
            new,
        )
        return f"{old} {new} refs/heads/main\n".encode()

    def _receive(self, view: str, updates: bytes, key: str | None) -> bool:
        """Run the receive contract; returns True when newly accepted."""
        environment = {
            "BASTION_VIEW": view,
            "BASTION_PRINCIPAL_ID": "alice",
            "BASTION_TRANSPORT": "test",
        }
        if key:
            environment["BASTION_IDEMPOTENCY_KEY"] = key
        before = Repository.open(self.fixture.root).load()[0].head_seq

        with patch_dict(environment):
            pre_receive(self.fixture.root, view, updates)
        after = Repository.open(self.fixture.root).load()[0].head_seq
        if after == before:
            return False
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

        manifest_before, _version, replay_before = Repository.open(self.fixture.root).load()

        # Replay of the identical request under the same key: recorded
        # outcome is returned and nothing is re-applied.
        self.assertFalse(self._receive("alice", updates, "key-1"))

        manifest_after, _version, replay_after = Repository.open(self.fixture.root).load()
        self.assertEqual(manifest_before.head_seq, manifest_after.head_seq)
        self.assertEqual(manifest_before.canonical_tip, manifest_after.canonical_tip)
        self.assertEqual(replay_before.events, replay_after.events)

    def test_recorded_outcome_persists_across_store_reopen(self) -> None:
        updates = self._updates({b"shared/restart.txt": (b"100644", b"restart\n")})
        self.assertTrue(self._receive("alice", updates, "persisted"))
        repository = Repository.open(self.fixture.root)
        _manifest, _version, replay = repository.load()
        record = replay.idempotency.get(
            idempotency_key_hash(repository.repository_id, "persisted")
        )
        self.assertIsNotNone(record)
        self.assertIn(record["event_id"], replay.events)

    def test_missing_key_behaves_normally(self) -> None:
        updates = self._updates({b"shared/plain.txt": (b"100644", b"plain\n")})
        self.assertTrue(self._receive("alice", updates, None))
        replay = Repository.open(self.fixture.root).load()[2]
        self.assertEqual(1, sum(record["kind"] == "submission" for record in replay.records))


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
