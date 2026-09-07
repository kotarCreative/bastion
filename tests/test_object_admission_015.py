"""Spec 015 A1: strict pre-publication object admission through intakes.

Malformed objects must be rejected during receive before any immutable
upload or manifest CAS, through real Git receive and the direct admission
path, on fresh and reused intake generations, leaving repository truth
unchanged and permitting subsequent valid pushes and fetches.
"""

from __future__ import annotations

import os
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge
import materialization
from errors import ForgeError
from layout import intake_repo
from protocol import pre_receive
from repository import Repository
from tests.storage_helpers import repository_state
from tests.transport import LoopbackTransport
from tests.tree_harness import TreeFixture, build_commit, command, git, oid

MALFORMED_TIMESTAMP = 1_700_000_500


def raw_commit(
    repo: Path, tree: str, parent: str, headers: list[str], message: str
) -> str:
    """Write a commit object with exactly the requested header lines."""
    content = (
        f"tree {tree}\nparent {parent}\n"
        + "\n".join(headers)
        + f"\n\n{message}\n"
    ).encode()
    return git(
        repo, "hash-object", "-t", "commit", "-w", "--literally", "--stdin", data=content
    ).stdout.decode().strip()


class ObjectAdmissionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)

    def malformed(self, variant: str) -> tuple[Path, str, str]:
        """Build a malformed commit in the alice client; return client, new, parent."""
        client = self.fixture.clients["alice"]
        parent = oid(client, "refs/heads/main")
        tree = git(client, "rev-parse", f"{parent}^{{tree}}").stdout.decode().strip()
        date = f"{MALFORMED_TIMESTAMP} +0000"
        author = f"author Fixture <tree@example.test> {date}"
        committer = f"committer Fixture <tree@example.test> {date}"
        second = f"author Second <second@example.test> {date}"
        if variant == "missing-committer":
            headers = [author]
        elif variant == "missing-author":
            headers = [committer]
        elif variant == "duplicate-author":
            headers = [author, committer, second]
        else:
            raise AssertionError(f"unknown variant {variant}")
        new = raw_commit(client, tree, parent, headers, f"{variant} change\n")
        git(client, "update-ref", "refs/heads/main", new, parent)
        return client, new, parent

    def push(self, client: Path) -> tuple[int, str]:
        pushed = command(
            "git", "-C", os.fsdecode(client), "push", "origin", "main", check=False
        )
        return pushed.returncode, pushed.stderr.decode(errors="replace")

    def valid_recovery(self, client: Path, parent: str) -> None:
        """Reset to the last good tip, push a valid commit, and fetch it."""
        git(client, "update-ref", "refs/heads/main", parent)
        build_commit(
            client,
            parent,
            {b"shared/recovered.txt": (b"100644", b"valid recovery\n")},
            b"valid recovery commit\n",
            MALFORMED_TIMESTAMP + 1,
        )
        code, output = self.push(client)
        self.assertEqual(code, 0, output)
        forge.drain_projection_jobs(self.fixture.root)

    def test_malformed_commits_rejected_through_real_local_receive(self) -> None:
        for variant in ("missing-committer", "missing-author", "duplicate-author"):
            with self.subTest(variant=variant):
                fixture = TreeFixture()
                self.addCleanup(fixture.close)
                self.fixture = fixture
                client, _new, parent = self.malformed(variant)
                before = fixture.rejection_snapshot()
                refs_before = fixture.refs()
                code, output = self.push(client)
                self.assertNotEqual(code, 0, output)
                manifest, replay = repository_state(fixture.root)
                self.assertEqual(
                    manifest.head_seq, repository_state(fixture.root)[0].head_seq
                )
                fixture.assert_rejection(before)
                self.assertEqual(fixture.refs(), refs_before)
                self.valid_recovery(client, parent)
                clone = fixture.directory / "recheck-alice"
                command(
                    "git",
                    "clone",
                    "-q",
                    "--no-local",
                    os.fsdecode(forge.view_repo(fixture.root, "alice")),
                    os.fsdecode(clone),
                )
                self.assertIn(b"valid recovery", (clone / "shared" / "recovered.txt").read_bytes())

    def test_malformed_commit_rejected_over_http(self) -> None:
        transport = LoopbackTransport(self.fixture.root)
        transport.attach(self.fixture.clients["alice"], "alice")
        try:
            client, _new, parent = self.malformed("duplicate-author")
            before = self.fixture.rejection_snapshot()
            code, output = self.push(client)
            self.assertNotEqual(code, 0, output)
            self.fixture.assert_rejection(before)
            self.valid_recovery(client, parent)
        finally:
            transport.stop()

    def test_reused_intake_enforces_receive_side_checks(self) -> None:
        client, _new, parent = self.malformed("duplicate-author")
        git(client, "update-ref", "refs/heads/main", parent)
        build_commit(
            client,
            parent,
            {b"shared/warm.txt": (b"100644", b"warm\n")},
            b"warm the intake\n",
            MALFORMED_TIMESTAMP + 1,
        )
        code, output = self.push(client)
        self.assertEqual(code, 0, output)
        forge.drain_projection_jobs(self.fixture.root)

        # A valid push keeps reusing the same intake generation.
        active = Path(os.path.realpath(intake_repo(self.fixture.root, "alice")))
        manifest, replay = repository_state(self.fixture.root)
        materialized = materialization.ensure_view_materialized(
            self.fixture.root, "alice", manifest, replay, Repository.open(self.fixture.root)
        )
        self.assertEqual(
            materialization.ensure_intake_materialized(
                self.fixture.root, materialized, manifest
            ),
            active,
        )

        # Config drift must not leave a reused intake without receive checks.
        git(active, "config", "--unset", "receive.fsckObjects")
        self.assertFalse(materialization._intake_receive_checks(active))
        repaired = materialization.ensure_intake_materialized(
            self.fixture.root, materialized, manifest
        )
        self.assertTrue(materialization._intake_receive_checks(repaired))

        client, _new, _parent = self.malformed("duplicate-author")
        before = self.fixture.rejection_snapshot()
        code, output = self.push(client)
        self.assertNotEqual(code, 0, output)
        self.fixture.assert_rejection(before)
        self.valid_recovery(client, _parent)

    def test_direct_admission_rejects_malformed_objects_without_quarantine(self) -> None:
        client, new, parent = self.malformed("duplicate-author")
        intake = intake_repo(self.fixture.root, "alice")
        command(
            "git", "--git-dir", os.fsdecode(intake), "fetch", "-q",
            os.fsdecode(client), "refs/heads/main",
        )
        before = self.fixture.rejection_snapshot()
        environment = {
            "BASTION_VIEW": "alice",
            "BASTION_PRINCIPAL_ID": "alice",
            "BASTION_TRANSPORT": "test",
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaises(ForgeError) as caught:
                pre_receive(self.fixture.root, "alice", f"{parent} {new} refs/heads/main\n".encode())
        self.assertIn("malformed", str(caught.exception))
        self.fixture.assert_rejection(before)

    def test_replay_after_cache_removal_stays_consistent(self) -> None:
        client = self.fixture.clients["alice"]
        build_commit(
            client,
            oid(client, "refs/heads/main"),
            {b"shared/after-cache.txt": (b"100644", b"cache\n")},
            b"cache removal setup\n",
            MALFORMED_TIMESTAMP + 2,
        )
        code, output = self.push(client)
        self.assertEqual(code, 0, output)
        forge.drain_projection_jobs(self.fixture.root)
        caches = self.fixture.root / "materializations"
        for name in ("canonical", "views", "intakes"):
            directory = caches / name
            if directory.exists():
                for entry in list(directory.iterdir()):
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
        manifest, replay = repository_state(self.fixture.root)
        materialization.ensure_view_materialized(
            self.fixture.root, "alice", manifest, replay, Repository.open(self.fixture.root)
        )
        materialization.ensure_intake_materialized(
            self.fixture.root,
            materialization.ensure_view_materialized(
                self.fixture.root, "alice", manifest, replay, Repository.open(self.fixture.root)
            ),
            manifest,
        )
        forge.check_invariants(self.fixture.root)


if __name__ == "__main__":
    unittest.main()
