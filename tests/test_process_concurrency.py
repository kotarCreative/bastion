"""Real-process concurrency against manifest-CAS publication."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge
from tests.storage_helpers import repository_state
from tests.transport import attach_intake_push
from tests.tree_harness import TreeFixture, build_commit, command, oid


@unittest.skipUnless(sys.platform.startswith("linux"), "process schedules require Linux")
class ProcessConcurrencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)

    def _push(self, checkout: Path) -> subprocess.Popen:
        return subprocess.Popen(
            ["git", "-C", str(checkout), "push", "-q", "origin", "main"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_same_view_competing_pushes_have_one_winner(self) -> None:
        second = self.fixture.directory / "alice-second"
        command(
            "git", "clone", "-q", "--no-local",
            forge.view_repo(self.fixture.root, "alice"), second,
        )
        attach_intake_push(self.fixture.root, second, "alice")
        command("git", "-C", second, "config", "user.name", "Alice")
        command("git", "-C", second, "config", "user.email", "alice@example.test")
        first = self.fixture.clients["alice"]
        first_old = oid(first, "refs/heads/main")
        build_commit(
            first, first_old,
            {b"shared/first.txt": (b"100644", b"first\n")},
            b"first\n", 1_700_000_100,
        )
        build_commit(
            second, first_old,
            {b"shared/second.txt": (b"100644", b"second\n")},
            b"second\n", 1_700_000_200,
        )

        processes = [self._push(first), self._push(second)]
        results = [process.communicate(timeout=60) + (process.returncode,) for process in processes]

        self.assertEqual([0, 1], sorted(0 if code == 0 else 1 for _out, _err, code in results))
        submissions = [
            record for record in repository_state(self.fixture.root)[1].records
            if record["kind"] == "submission"
        ]
        self.assertEqual(1, len(submissions))

    def test_hidden_disjoint_cross_view_pushes_both_commit(self) -> None:
        alice = self.fixture.clients["alice"]
        bob = self.fixture.clients["bob"]
        build_commit(
            alice, oid(alice, "refs/heads/main"),
            {b"team-a/process.txt": (b"100644", b"alice\n")},
            b"alice private\n", 1_700_000_300,
        )
        build_commit(
            bob, oid(bob, "refs/heads/main"),
            {b"team-b/process.txt": (b"100644", b"bob\n")},
            b"bob private\n", 1_700_000_400,
        )

        processes = [self._push(alice), self._push(bob)]
        results = [process.communicate(timeout=60) + (process.returncode,) for process in processes]

        self.assertTrue(all(code == 0 for _out, _err, code in results), results)
        replay = repository_state(self.fixture.root)[1]
        submissions = [record for record in replay.records if record["kind"] == "submission"]
        self.assertEqual(2, len(submissions))
        forge.sync_view(self.fixture.root, "alice")
        forge.sync_view(self.fixture.root, "bob")
        canonical = forge.canonical_repo(self.fixture.root)
        self.assertEqual(b"alice\n", forge.git(canonical, "show", "main:team-a/process.txt").stdout)
        self.assertEqual(b"bob\n", forge.git(canonical, "show", "main:team-b/process.txt").stdout)

    def test_concurrent_workers_converge_on_one_manifest_generation(self) -> None:
        processes = [
            subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve().parent.parent / "forge.py"),
                 "work", str(self.fixture.root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        results = [process.communicate(timeout=60) + (process.returncode,) for process in processes]
        self.assertTrue(all(code == 0 for _out, _err, code in results), results)
        forge.check_invariants(self.fixture.root)


if __name__ == "__main__":
    unittest.main()
