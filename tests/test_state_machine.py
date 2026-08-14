from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.state_machine import (
    Operation,
    Scenario,
    filtered,
    replay_scenario,
    run_scenario,
    tree_at,
    under_prefix,
    _format_failure,
)


SMOKE_SEEDS = (0x5EED, 0xC0FFEE)
REGRESSION_SEEDS: tuple[int, ...] = ()
DEFAULT_STEPS = 24


def profile_from_environment() -> tuple[tuple[int, ...], int, bool]:
    seeds_text = os.environ.get("VEIL_STATE_MACHINE_SEEDS")
    steps_text = os.environ.get("VEIL_STATE_MACHINE_STEPS")
    overridden = seeds_text is not None or steps_text is not None
    if seeds_text is None:
        seeds = SMOKE_SEEDS
    else:
        try:
            seeds = tuple(int(value.strip(), 0) for value in seeds_text.split(",") if value.strip())
        except ValueError as error:
            raise AssertionError("VEIL_STATE_MACHINE_SEEDS must be comma-separated integers") from error
        if not seeds:
            raise AssertionError("VEIL_STATE_MACHINE_SEEDS must contain at least one integer")
    if steps_text is None:
        steps = DEFAULT_STEPS
    else:
        try:
            steps = int(steps_text)
        except ValueError as error:
            raise AssertionError("VEIL_STATE_MACHINE_STEPS must be a positive integer") from error
        if steps <= 0:
            raise AssertionError("VEIL_STATE_MACHINE_STEPS must be a positive integer")
    return tuple(dict.fromkeys((*seeds, *REGRESSION_SEEDS))), steps, overridden


class StateMachineTest(unittest.TestCase):
    def test_prefix_filter_is_independent_and_boundary_aware(self) -> None:
        self.assertTrue(under_prefix(b"shared", ["shared"]))
        self.assertTrue(under_prefix(b"shared/a.txt", ["shared"]))
        self.assertFalse(under_prefix(b"sharedness/a.txt", ["shared"]))
        tree = {b"shared/a": (b"100644", b"a"), b"hidden/a": (b"100644", b"h")}
        self.assertEqual({b"shared/a": (b"100644", b"a")}, filtered(tree, ["shared"]))

    def test_tree_reader_preserves_bytes_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scenario = Scenario(1, 1, Path(temporary))
            oid = scenario._refs()["canonical"]["refs/heads/main"]
            self.assertEqual(scenario.canonical_tree, tree_at(scenario.root / "canonical.git", oid))
            self.assertEqual((b"100644", b"hidden secret\n"), tree_at(scenario.root / "canonical.git", oid)[b"hidden/secret.txt"])

    def test_administrative_visible_and_hidden_updates_follow_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scenario = Scenario(7, 1, Path(temporary))
            alice_before = scenario._refs()["alice"]["refs/heads/main"]
            visible = {"path": "shared/a.txt", "action": "create", "contents": "visible\n"}
            scenario.execute(Operation("admin_edit", visible), 0)
            self.assertEqual(alice_before, scenario._refs()["alice"]["refs/heads/main"])
            scenario.execute(Operation("worker_complete", {}), 0)
            scenario.execute(Operation("worker_complete", {}), 0)
            self.assertNotEqual(alice_before, scenario._refs()["alice"]["refs/heads/main"])
            alice_tip = scenario._refs()["alice"]["refs/heads/main"]
            hidden = {"path": "hidden/a.txt", "action": "create", "contents": "hidden\n"}
            scenario.execute(Operation("admin_edit", hidden), 0)
            scenario.execute(Operation("worker_complete", {}), 0)
            scenario.execute(Operation("worker_complete", {}), 0)
            self.assertEqual(alice_tip, scenario._refs()["alice"]["refs/heads/main"])

    def test_two_commit_source_push_preserves_submitted_tip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            scenario = Scenario(9, 1, Path(temporary))
            first = {"path": "shared/a.txt", "action": "create", "contents": "first\n", "change_id": "I" + "1" * 40}
            second = {"path": "team-a/a.txt", "action": "create", "contents": "second\n"}
            scenario.execute(Operation("authorized_push", {"view": "alice", "edits": [first, second]}), 0)
            state = json.loads((scenario.root / "state.json").read_text())
            transaction = next(tx for tx in state["transactions"].values() if tx.get("view") == "alice")
            self.assertEqual(2, len(transaction["mappings"]))
            self.assertEqual(scenario.clients["alice"]["oid"], scenario._refs()["alice"]["refs/heads/main"])

    def test_seed_and_direct_trace_replay_are_exact(self) -> None:
        first = run_scenario(SMOKE_SEEDS[0], 3)
        second = run_scenario(SMOKE_SEEDS[0], 3)
        replayed = replay_scenario(SMOKE_SEEDS[0], first.trace)
        self.assertEqual(first.trace, second.trace)
        self.assertEqual(first.refs, second.refs)
        self.assertEqual(first.state_bytes, second.state_bytes)
        self.assertEqual(first.refs, replayed.refs)
        self.assertEqual(first.state_bytes, replayed.state_bytes)

    def test_default_profile_has_required_operation_and_edit_coverage(self) -> None:
        seeds, steps, overridden = profile_from_environment()
        results = []
        for seed in seeds:
            with self.subTest(seed=seed):
                results.append(run_scenario(seed, steps))
        if overridden:
            return
        operations = [operation for result in results for operation in result.trace]
        kinds = {operation.kind for operation in operations}
        required = {
            "admin_edit", "authorized_push", "unauthorized_push", "stale_push", "client_refresh",
            "server_sync", "add_view", "worker_complete", "worker_fail_then_retry",
            "worker_crash_before_ref", "recover_expired_worker", "worker_crash_after_ref_and_recover",
            "source_crash", "source_barrier_probes", "source_retry",
        }
        self.assertTrue(required <= kinds)
        edits = [item for operation in operations for item in operation.arguments.get("edits", [])]
        edits.extend(operation.arguments for operation in operations if operation.kind == "admin_edit")
        edits.extend(operation.arguments["edit"] for operation in operations if operation.kind == "source_crash")
        self.assertEqual({"create", "modify", "delete"}, {item["action"] for item in edits})
        accepted = next(operation for operation in operations if operation.kind == "authorized_push")
        self.assertTrue(any(edit.get("change_id") for edit in accepted.arguments["edits"]))
        self.assertTrue(any(not edit.get("change_id") for operation in operations if operation.kind == "source_crash" for edit in [operation.arguments["edit"]]))

    def test_environment_profile_validation_and_explicit_replay(self) -> None:
        with patch.dict(os.environ, {"VEIL_STATE_MACHINE_SEEDS": "0x1234", "VEIL_STATE_MACHINE_STEPS": "2"}, clear=False):
            seeds, steps, overridden = profile_from_environment()
        self.assertEqual((0x1234,), seeds)
        self.assertEqual(2, steps)
        self.assertTrue(overridden)
        with patch.dict(os.environ, {"VEIL_STATE_MACHINE_STEPS": "0"}, clear=False):
            with self.assertRaisesRegex(AssertionError, "positive integer"):
                profile_from_environment()

    def test_failure_trace_is_structured_and_deterministic(self) -> None:
        trace = [Operation("recover_dispatch", {"before_step": 0}), Operation("admin_edit", {"path": "x"})]
        message = str(_format_failure(12, 3, 1, trace[-1], trace, AssertionError("injected")))
        self.assertIn("state-machine failure\nseed: 12\nsteps: 3\nfailed step: 1", message)
        self.assertIn('0001 admin_edit {"path": "x"}', message)
        self.assertTrue(message.endswith("injected"))


if __name__ == "__main__":
    unittest.main()
