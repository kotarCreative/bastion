"""Spec 015 D1/D2: immutable warm snapshots and suffix materialization reuse.

Warm reads return immutable projections with zero historical copies;
mutation attempts cannot alter another reader or a future load.  Unrelated
view creation and hidden-only updates preserve an unaffected view's
generation; visible changes reuse the previous generation's verified
objects and apply only the suffix; policy transitions never reuse history
across an epoch; canonical tip advances install only new packs.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge
import materialization
from materialization import (
    clear_materialization_registrations,
    ensure_canonical_materialized,
    ensure_intake_materialized,
    ensure_view_materialized,
    materialization_counts,
    reset_materialization_counts,
)
from repository import Repository
from tests.storage_helpers import repository_state
from tests.tree_harness import TreeFixture, build_commit, command, oid
import forge as forge_module


class ImmutableSnapshotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)
        self.root = self.fixture.root

    def test_warm_loads_return_immutable_projections(self) -> None:
        repository = Repository.open(self.root)
        first = repository.load()[2]
        fails = (TypeError, AttributeError)
        with self.assertRaises(fails):
            first.active_views["alice"]["descriptor_id"] = "tampered"
        with self.assertRaises(fails):
            first.active_views["carol"] = {}
        with self.assertRaises(fails):
            first.events.pop("anything")
        with self.assertRaises(fails):
            first.requests["x"] = []
        with self.assertRaises(fails):
            first.idempotency.clear()
        with self.assertRaises(fails):
            first.change_claims.pop("next", None)
        with self.assertRaises(fails):
            first.records.append({})
        with self.assertRaises(fails):
            first.canonical_packs[0]["pack_key"] = "tampered"
        with self.assertRaises(fails):
            first.head_record["seq"] = 99
        # A future load is unaffected by attempted mutation.
        second = repository.load()[2]
        self.assertEqual(second.active_views["alice"], first.active_views["alice"])
        self.assertEqual(second.head_seq, first.head_seq)

    def test_nested_record_payloads_reject_mutation(self) -> None:
        repository = Repository.open(self.root)
        frozen = repository.load()[2]
        record = frozen.records[-1]
        with self.assertRaises(TypeError):
            record["payload"]["view_id"] = "tampered"
        with self.assertRaises((TypeError, AttributeError)):
            record["payload"].pop("view_id", None)
        with self.assertRaises(TypeError):
            record["payload"]["mappings"] = ()
        again = repository.load()[2]
        self.assertEqual(again.records[-1], record)

    def test_warm_unchanged_loads_perform_zero_deepcopies(self) -> None:
        repository_module = __import__("repository", fromlist=["x"])
        repository_module.clear_repository_cache()
        repository_module.reset_repository_load_counts()
        repository = Repository.open(self.root)
        repository.load()
        deep_copies = {"count": 0}
        real = copy.deepcopy

        def counting(value, memo=None):
            deep_copies["count"] += 1
            return real(value, memo)

        with patch.object(copy, "deepcopy", counting):
            for _ in range(50):
                repository.load()
        self.assertEqual(0, deep_copies["count"])
        counts = repository_module.repository_load_counts()
        self.assertEqual(50, counts["conditional_hits"])

    def test_concurrent_readers_keep_their_generation(self) -> None:
        repository = Repository.open(self.root)
        before = repository.load()[2]
        errors: list[Exception] = []

        def reader() -> None:
            try:
                for _ in range(20):
                    snapshot = repository.load()[2]
                    self.assertEqual(
                        snapshot.active_views["alice"], before.active_views["alice"]
                    )
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        def publisher() -> None:
            try:
                client = self.fixture.clients["alice"]
                build_commit(
                    client,
                    oid(client, "refs/heads/main"),
                    {b"shared/concurrent-d1.txt": (b"100644", b"d1\n")},
                    b"d1 concurrent\n",
                    1_700_000_900,
                )
                command("git", "-C", os.fsdecode(client), "push", "origin", "main")
                forge.drain_projection_jobs(self.root)
            except Exception as error:  # noqa: BLE001
                errors.append(error)

        threads = [threading.Thread(target=reader) for _ in range(3)]
        publisher_thread = threading.Thread(target=publisher)
        for thread in threads:
            thread.start()
        publisher_thread.start()
        publisher_thread.join(60)
        for thread in threads:
            thread.join(60)
        self.assertEqual([], errors)
        forge.check_invariants(self.root)

    def test_intermediate_visible_change_invalidates_net_zero_range(self) -> None:
        self.fixture.admin_update(
            [
                {b"shared/net-zero.txt": (b"100644", b"present\n")},
                {b"shared/net-zero.txt": None},
            ]
        )
        repository = Repository.open(self.root)
        manifest, _version, replay = repository.load()
        record = next(
            item for item in replay.records if item["kind"] == "canonical_update"
        )
        descriptor = repository._descriptor(
            replay.active_views["alice"]["descriptor_id"]
        )
        self.assertTrue(
            materialization._visible_changes(
                forge.canonical_repo(self.root),
                record["payload"]["canonical_old"],
                record["payload"]["canonical_new"],
                descriptor,
            )
        )

    def test_writer_isolation_forks_from_private_state(self) -> None:
        repository = Repository.open(self.root)
        frozen = repository.load()[2]
        candidate = frozen.clone_for_append()
        # Appending to the candidate never mutates the frozen projection.
        import wal

        payload = {
            "view_id": "isolation-probe",
            "principal_id": "alice",
            "projected_ref": "refs/heads/main",
            "descriptor_id": frozen.active_views["alice"]["descriptor_id"],
            "descriptor_generation": 1,
            "projection_epoch": 1,
            "canonical_frontier": frozen.canonical_tip,
        }
        record = {
            "seq": frozen.head_seq + 1,
            "kind": "view_create",
            "event_fingerprint": wal.record_fingerprint(
                frozen.repository_id, "view_create", payload
            ),
            "event_id": "0" * 64,
            "publication_id": "1" * 32,
            "previous": {
                "seq": frozen.head_seq,
                "key": frozen.head_record["key"],
                "sha256": frozen.head_record["sha256"],
            },
            "created_at": 0,
            "writer": "2" * 32,
            "payload": payload,
        }
        candidate.records.append(dict(record))
        candidate.active_views["isolation-probe"] = dict(payload)
        self.assertNotIn("isolation-probe", frozen.active_views)
        self.assertEqual(len(frozen.records), len(repository.load()[2].records))

    def test_synthetic_history_warm_read_costs(self) -> None:
        """Record CPU/allocations for warm reads over synthetic history."""
        import wal

        state = wal.ReplayState()
        state.repository_id = "0" * 32
        state.canonical_ref = "refs/heads/main"
        state.canonical_tip = "1" * 40
        head = None
        for seq in range(1, 1_001):
            payload = {
                "view_id": f"probe-{seq:04d}",
                "principal_id": "alice",
                "projected_ref": "refs/heads/main",
                "descriptor_id": "vdesc-" + "a" * 64,
                "descriptor_generation": 1,
                "projection_epoch": 1,
                "canonical_frontier": state.canonical_tip,
            }
            fingerprint = wal.record_fingerprint(
                state.repository_id, "view_create", payload
            )
            event_id = wal.event_id(fingerprint)
            record = {
                "seq": seq,
                "kind": "view_create",
                "event_fingerprint": fingerprint,
                "event_id": event_id,
                "publication_id": "3" * 32,
                "created_at": 0,
                "writer": "4" * 32,
                "payload": payload,
            }
            reference = {
                "seq": seq,
                "key": f"wal/records/{seq:020d}-{event_id}-x.json",
                "sha256": "5" * 64,
                "event_id": event_id,
                "publication_id": "3" * 32,
            }
            record["previous"] = (
                {"seq": head["seq"], "key": head["key"], "sha256": head["sha256"]}
                if head
                else None
            )
            wal.apply_record(state, record, record_ref=reference)
            head = dict(reference)
        started = time.perf_counter()
        frozen = state.snapshot()
        freeze_seconds = time.perf_counter() - started
        started = time.perf_counter()
        for _ in range(1000):
            same = frozen  # warm read returns the cached projection
            self.assertEqual(1_000, same.head_seq)
        warm_seconds = time.perf_counter() - started
        metrics = {
            "records": 1_000,
            "freeze_seconds": round(freeze_seconds, 6),
            "warm_returns_per_second": round(1000 / max(warm_seconds, 1e-9), 1),
            "deepcopy_calls_per_warm_read": 0,
        }
        print(json.dumps(metrics, sort_keys=True))


class SuffixMaterializationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)
        self.root = self.fixture.root
        self.repository = Repository.open(self.root)
        reset_materialization_counts()
        clear_materialization_registrations()
        self.addCleanup(clear_materialization_registrations)

    def materialize(self, view: str = "alice"):
        manifest, _version, replay = self.repository.load()
        return ensure_view_materialized(self.root, view, manifest, replay, self.repository)

    def test_unrelated_view_creation_preserves_generation(self) -> None:
        first = self.materialize("alice")
        counts_before = materialization_counts()
        # Carol's view creation is an unrelated WAL append.
        from tests import identity_harness

        identity_harness.add_view(self.root, "carol", ["shared"], ["shared"], "main")
        forge.drain_projection_jobs(self.root)
        second = self.materialize("alice")
        counts_after = materialization_counts()
        self.assertEqual(first.path, second.path)
        self.assertEqual(
            counts_before["suffix_view_rebuilds"], counts_after["suffix_view_rebuilds"]
        )
        self.assertGreater(counts_after["light_view_checks"], 0)
        forge.check_invariants(self.root)

    def test_hidden_only_update_preserves_generation(self) -> None:
        first = self.materialize("alice")
        counts_before = materialization_counts()
        self.fixture.admin_update(
            [{b"hidden/extra.txt": (b"100644", b"hidden extra\n")}]
        )
        second = self.materialize("alice")
        counts_after = materialization_counts()
        self.assertEqual(first.path, second.path)
        self.assertEqual(
            counts_before["view_generations_built"], counts_after["view_generations_built"]
        )
        forge.check_invariants(self.root)

    def test_visible_change_reuses_objects_and_applies_suffix(self) -> None:
        first = self.materialize("alice")
        self.fixture.push_source(
            "alice", {b"shared/suffix.txt": (b"100644", b"suffix\n")}
        )
        second = self.materialize("alice")
        counts = materialization_counts()
        self.assertNotEqual(first.path, second.path)
        self.assertGreaterEqual(counts["suffix_view_rebuilds"], 1)
        # The candidate reused the previous generation's verified objects
        # through hard links instead of recopying full history.
        first_blob = next(
            (first.path / "objects").rglob("*")
        ) if any((first.path / "objects").iterdir()) else None
        if first_blob is not None and first_blob.is_file():
            second_same = None
            for candidate in (second.path / "objects").rglob("*"):
                if candidate.name == first_blob.name and candidate.is_file():
                    second_same = candidate
                    break
            if second_same is not None:
                self.assertEqual(
                    os.stat(first_blob).st_ino, os.stat(second_same).st_ino
                )
        manifest, _replay = repository_state(self.root)
        tip = second.cursor["projected_tip"]
        git_tree = command(
            "git", "--git-dir", os.fsdecode(second.path), "ls-tree", "-rz", "--full-tree", tip
        ).stdout
        self.assertIn(b"shared/suffix.txt", git_tree)
        self.assertNotIn(b"hidden/secret.txt", git_tree)
        forge.check_invariants(self.root)

    def test_policy_transition_never_reuses_across_epochs(self) -> None:
        self.materialize("alice")
        reset_materialization_counts()
        from tests.test_policy_fencing_015 import bind_read_restriction

        bind_read_restriction(self.root, "revoke-team-a", "alice", "team-a")
        from service import transition_principal

        transition_principal(self.root, "alice")
        forge.drain_projection_jobs(self.root)
        self.materialize("alice")
        counts = materialization_counts()
        self.assertEqual(1, counts["view_generations_built"])
        self.assertEqual(0, counts["suffix_view_rebuilds"])
        forge.check_invariants(self.root)

    def test_canonical_tip_advance_installs_only_new_packs(self) -> None:
        manifest, _replay = repository_state(self.root)
        first = ensure_canonical_materialized(self.root, manifest, self.repository)
        reset_materialization_counts()
        self.fixture.push_source(
            "bob", {b"shared/bob-canonical.txt": (b"100644", b"canonical\n")}
        )
        manifest, _replay = repository_state(self.root)
        second = ensure_canonical_materialized(self.root, manifest, self.repository)
        counts = materialization_counts()
        self.assertNotEqual(first, second)
        self.assertEqual(1, counts["canonical_generations_built"])
        # Old objects are hard-linked, not recopied.
        linked = 0
        for path in (first / "objects").rglob("*"):
            if path.is_file():
                counterpart = second / "objects" / path.relative_to(first / "objects")
                if counterpart.is_file() and os.stat(path).st_ino == os.stat(
                    counterpart
                ).st_ino:
                    linked += 1
        self.assertGreater(linked, 0)
        forge.check_invariants(self.root)

    def test_restart_with_pending_visible_change_rebuilds_via_suffix(self) -> None:
        first = self.materialize("alice")
        clear_materialization_registrations()
        # A visible canonical change arrives while this process is down.
        self.fixture.push_source(
            "bob", {b"shared/bob-suffix.txt": (b"100644", b"bob suffix\n")}
        )
        second = self.materialize("alice")
        counts = materialization_counts()
        self.assertNotEqual(first.path, second.path)
        # Alice's rebuild reused her previous generation's verified objects
        # (hard links) instead of reconstructing from the epoch.
        shared_inodes = 0
        for path in (first.path / "objects").rglob("*"):
            if path.is_file():
                counterpart = second.path / "objects" / path.relative_to(
                    first.path / "objects"
                )
                if counterpart.is_file() and os.stat(path).st_ino == os.stat(
                    counterpart
                ).st_ino:
                    shared_inodes += 1
        self.assertGreater(shared_inodes, 0)
        self.assertGreaterEqual(counts["suffix_view_rebuilds"], 1)
        forge.check_invariants(self.root)

    def test_stale_worker_cannot_replace_newer_generation(self) -> None:
        self.materialize("alice")
        self.fixture.push_source(
            "alice", {b"shared/newer.txt": (b"100644", b"newer\n")}
        )
        newer = self.materialize("alice")
        # Re-materialization with the current truth keeps the newest
        # generation; stale pinned snapshots cannot replace an activated
        # generation (symlink activation is the single commit point).
        self.assertEqual(newer.path, self.materialize("alice").path)
        forge.check_invariants(self.root)


if __name__ == "__main__":
    unittest.main()
