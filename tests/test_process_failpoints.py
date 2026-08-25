"""Real-process crash checks at WAL publication boundaries."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge
from tests.storage_helpers import repository_state
from tests.tree_harness import TreeFixture, build_commit, command, oid


PRE_COMMIT = (
    "wal.source_materialized",
    "wal.validation_complete",
    "wal.canonical_pack_built",
    "wal.submission_pack_built",
    "wal.canonical_pack_uploaded",
    "wal.submission_pack_uploaded",
    "wal.record_uploaded",
    "wal.manifest_cas_sending",
)
POST_COMMIT = (
    "wal.manifest_cas_committed",
    "wal.git_success_returning",
)


@unittest.skipUnless(
    sys.platform.startswith("linux") and Path("/proc/self/stat").exists(),
    "failpoint crashes require Linux /proc",
)
class ProcessFailpointTest(unittest.TestCase):
    def _crash_push(self, checkpoint: str) -> tuple[TreeFixture, int]:
        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        client = fixture.clients["alice"]
        build_commit(
            client,
            oid(client, "refs/heads/main"),
            {b"shared/failpoint.txt": (b"100644", checkpoint.encode() + b"\n")},
            f"fail at {checkpoint}\n".encode(),
            1_700_000_500,
        )
        before = repository_state(fixture.root)[0].head_seq
        marker = fixture.directory / "failpoint.json"
        environment = os.environ | {
            "BASTION_TEST_FAILPOINT": checkpoint,
            "BASTION_TEST_FAILPOINT_ROOT": str(fixture.root),
            "BASTION_TEST_FAILPOINT_MARKER": str(marker),
        }
        process = subprocess.Popen(
            ["git", "-C", str(client), "push", "-q", "origin", "main"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        deadline = time.time() + 20
        while not marker.exists() and process.poll() is None and time.time() < deadline:
            time.sleep(0.02)
        if not marker.exists():
            output, error = process.communicate(timeout=5)
            self.fail(f"checkpoint {checkpoint} was not reached: {output!r} {error!r}")
        stopped = json.loads(marker.read_text())
        os.kill(stopped["pid"], signal.SIGKILL)
        process.communicate(timeout=20)
        return fixture, before

    def test_push_crash_matrix_uses_manifest_reachability_for_commitment(self) -> None:
        for checkpoint in (*PRE_COMMIT, *POST_COMMIT):
            with self.subTest(checkpoint=checkpoint):
                fixture, before = self._crash_push(checkpoint)
                committed = repository_state(fixture.root)[0].head_seq
                expected = before + (1 if checkpoint in POST_COMMIT else 0)
                self.assertEqual(expected, committed)

                retry = command(
                    "git", "-C", fixture.clients["alice"],
                    "push", "-q", "origin", "main", check=False,
                )
                self.assertEqual(0, retry.returncode, retry.stderr.decode())
                manifest, replay = repository_state(fixture.root)
                self.assertEqual(before + 1, manifest.head_seq)
                self.assertEqual(
                    1, sum(record["kind"] == "submission" for record in replay.records)
                )

    def test_crash_after_secondary_materialization_loses_only_job_state(self) -> None:
        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        client = fixture.clients["alice"]
        build_commit(
            client,
            oid(client, "refs/heads/main"),
            {b"shared/secondary.txt": (b"100644", b"secondary\n")},
            b"secondary\n",
            1_700_000_600,
        )
        command("git", "-C", client, "push", "-q", "origin", "main")
        committed = repository_state(fixture.root)[0].head_seq
        marker = fixture.directory / "secondary.json"
        environment = os.environ | {
            "BASTION_TEST_FAILPOINT": "wal.secondary_materialized",
            "BASTION_TEST_FAILPOINT_ROOT": str(fixture.root),
            "BASTION_TEST_FAILPOINT_MARKER": str(marker),
        }
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parent.parent / "forge.py"),
             "work", str(fixture.root), "--limit", "1"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
        )
        deadline = time.time() + 20
        while not marker.exists() and process.poll() is None and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(marker.exists())
        stopped = json.loads(marker.read_text())
        os.kill(stopped["pid"], signal.SIGKILL)
        process.communicate(timeout=20)
        self.assertEqual(committed, repository_state(fixture.root)[0].head_seq)

        command(
            sys.executable,
            Path(__file__).resolve().parent.parent / "forge.py",
            "work",
            fixture.root,
        )
        forge.check_invariants(fixture.root)


if __name__ == "__main__":
    unittest.main()
