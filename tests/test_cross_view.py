from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import forge
from repository import Repository
from tests import identity_harness
from errors import ForgeError
from tests.storage_helpers import repository_state, submission_record
from tests.transport import attach_intake_push




def command(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        args,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ | (env or {}),
    )
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def git(
    repo: Path,
    *args: str,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return command("git", *args, cwd=repo, check=check, env=env)


def commit(repo: Path, message: str, timestamp: int) -> str:
    git(repo, "add", "-A")
    git(
        repo,
        "commit",
        "-q",
        "-m",
        message,
        env={
            "GIT_AUTHOR_DATE": f"{timestamp} +0000",
            "GIT_COMMITTER_DATE": f"{timestamp} +0000",
        },
    )
    return git(repo, "rev-parse", "HEAD").stdout.strip()


class CrossViewForgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        git(self.root, "init", "-q", "-b", "main", str(source))
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        for path, contents in (
            ("shared/base.txt", "shared base\n"),
            ("team-a/private.txt", "team a\n"),
            ("team-b/private.txt", "team b\n"),
            ("hidden/secret.txt", "secret\n"),
        ):
            target = source / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(contents)
        commit(source, "initial mixed views", 1_710_000_000)

        self.forge = self.root / "forge"
        forge.init_forge(self.forge, source, "main")
        identity_harness.add_view(
            self.forge,
            "alice",
            ["shared", "team-a"],
            ["shared", "team-a"],
            "main",
        )
        identity_harness.add_view(
            self.forge,
            "bob",
            ["shared", "team-b"],
            ["shared", "team-b"],
            "main",
        )
        self.alice_remote = forge.view_repo(self.forge, "alice")
        self.bob_remote = forge.view_repo(self.forge, "bob")
        self.alice = self.clone("alice", self.alice_remote)
        self.bob = self.clone("bob", self.bob_remote)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def clone(self, name: str, remote: Path) -> Path:
        checkout = self.root / name
        command("git", "clone", "-q", "--no-local", str(remote), str(checkout))
        attach_intake_push(self.forge, checkout, name)
        git(checkout, "config", "user.name", name.title())
        git(checkout, "config", "user.email", f"{name}@example.test")
        return checkout

    def remote_tip(self, remote: Path) -> str:
        return forge.ref(remote, "refs/heads/main")

    def assert_hidden_objects_absent(self, remote: Path, *paths: str) -> None:
        canonical = forge.canonical_repo(self.forge)
        reachable = forge.output(remote, "rev-list", "--objects", "--all").split()
        for path in paths:
            oid = forge.output(canonical, "rev-parse", f"main:{path}")
            self.assertNotIn(oid, reachable)
            self.assertNotEqual(
                0,
                forge.git(remote, "cat-file", "-e", oid, check=False).returncode,
            )

    def test_overlapping_views_publish_build_and_rebase(self) -> None:
        alice_initial = self.remote_tip(self.alice_remote)
        bob_initial = self.remote_tip(self.bob_remote)
        team_change = "Iaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        shared_change = "Ibbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

        (self.alice / "team-a" / "private.txt").write_text("team a updated\n")
        alice_private = commit(
            self.alice,
            f"alice private\n\nChange-Id: {team_change}",
            1_710_000_100,
        )
        (self.alice / "shared" / "alice.txt").write_text("alice shared\n")
        alice_shared = commit(
            self.alice,
            f"alice shared\n\nChange-Id: {shared_change}",
            1_710_000_200,
        )
        git(self.alice, "push", "-q", "origin", "main")
        forge.drain_projection_jobs(self.forge)

        self.assertEqual(alice_shared, self.remote_tip(self.alice_remote))
        bob_after_alice = self.remote_tip(self.bob_remote)
        self.assertNotEqual(bob_initial, bob_after_alice)
        self.assertNotEqual(alice_shared, bob_after_alice)
        self.assertEqual(
            1,
            int(
                forge.output(
                    self.bob_remote, "rev-list", "--count", f"{bob_initial}..main"
                )
            ),
        )
        self.assertIn(
            f"Change-Id: {shared_change}",
            forge.output(self.bob_remote, "show", "-s", "--format=%B", "main"),
        )
        self.assertEqual(
            "2000-01-01T00:00:00Z",
            forge.output(self.bob_remote, "show", "-s", "--format=%aI", "main"),
        )
        self.assertNotIn(
            team_change,
            forge.output(self.bob_remote, "log", "--format=%B", "--all"),
        )
        self.assert_hidden_objects_absent(
            self.bob_remote, "team-a/private.txt", "hidden/secret.txt"
        )

        source_record = submission_record(
            self.forge, "alice", alice_initial, alice_shared
        )
        self.assertEqual(
            [team_change, shared_change],
            [mapping["change_id"] for mapping in source_record["payload"]["mappings"]],
        )

        git(self.bob, "fetch", "-q", "origin")
        git(self.bob, "reset", "-q", "--hard", "origin/main")
        (self.bob / "shared" / "bob.txt").write_text("bob shared\n")
        bob_change = "Icccccccccccccccccccccccccccccccccccccccc"
        bob_submitted = commit(
            self.bob,
            f"bob builds on alice\n\nChange-Id: {bob_change}",
            1_710_000_300,
        )
        git(self.bob, "push", "-q", "origin", "main")
        forge.drain_projection_jobs(self.forge)
        self.assertEqual(bob_submitted, self.remote_tip(self.bob_remote))
        alice_after_bob = self.remote_tip(self.alice_remote)
        self.assertNotEqual(alice_shared, alice_after_bob)
        self.assertNotEqual(bob_submitted, alice_after_bob)

        git(self.alice, "fetch", "-q", "origin")
        git(self.alice, "reset", "-q", "--hard", "origin/main")
        self.assertEqual("alice shared\n", (self.alice / "shared" / "alice.txt").read_text())
        self.assertEqual("bob shared\n", (self.alice / "shared" / "bob.txt").read_text())
        self.assertEqual("team a updated\n", (self.alice / "team-a" / "private.txt").read_text())
        self.assert_hidden_objects_absent(
            self.alice_remote, "team-b/private.txt", "hidden/secret.txt"
        )

        (self.alice / "shared" / "concurrent-alice.txt").write_text("alice concurrent\n")
        alice_concurrent = commit(self.alice, "alice concurrent", 1_710_000_400)
        (self.bob / "shared" / "concurrent-bob.txt").write_text("bob concurrent\n")
        bob_before_rebase = commit(self.bob, "bob concurrent", 1_710_000_500)

        git(self.alice, "push", "-q", "origin", "main")
        forge.drain_projection_jobs(self.forge)
        self.assertEqual(alice_concurrent, self.remote_tip(self.alice_remote))
        rejected = git(self.bob, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("fetch first", rejected.stderr)

        git(self.bob, "fetch", "-q", "origin")
        git(self.bob, "rebase", "origin/main")
        bob_rebased = git(self.bob, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(bob_before_rebase, bob_rebased)
        git(self.bob, "push", "-q", "origin", "main")
        forge.drain_projection_jobs(self.forge)
        self.assertEqual(bob_rebased, self.remote_tip(self.bob_remote))
        canonical = forge.canonical_repo(self.forge)
        self.assertEqual(
            b"alice concurrent\n",
            forge.git(canonical, "show", "main:shared/concurrent-alice.txt").stdout,
        )
        self.assertEqual(
            b"bob concurrent\n",
            forge.git(canonical, "show", "main:shared/concurrent-bob.txt").stdout,
        )
        self.assertNotEqual(alice_initial, self.remote_tip(self.alice_remote))

    def test_missing_secondary_cache_is_reconstructed_from_wal(self) -> None:
        (self.alice / "shared" / "recover.txt").write_text("recover cross view\n")
        submitted = commit(
            self.alice,
            "cross-view recovery",
            1_710_000_600,
        )
        git(self.alice, "push", "-q", "origin", "main")
        self.bob_remote.unlink()

        forge.sync_view(self.forge, "bob")
        forge.sync_view(self.forge, "alice")

        self.assertEqual(
            b"recover cross view\n",
            forge.git(self.bob_remote, "show", "main:shared/recover.txt").stdout,
        )
        self.assertEqual(submitted, self.remote_tip(self.alice_remote))

    def test_change_id_cannot_be_claimed_by_another_principal(self) -> None:
        change_id = "Ieeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
        (self.alice / "shared" / "owned.txt").write_text("owned by alice\n")
        commit(
            self.alice,
            f"alice owns logical change\n\nChange-Id: {change_id}",
            1_710_000_700,
        )
        git(self.alice, "push", "-q", "origin", "main")
        forge.sync_view(self.forge, "bob")
        canonical_before = repository_state(self.forge)[0].canonical_tip

        git(self.bob, "fetch", "-q", "origin")
        git(self.bob, "reset", "-q", "--hard", "origin/main")
        (self.bob / "shared" / "claim.txt").write_text("claimed by bob\n")
        commit(
            self.bob,
            f"bob attempts claim\n\nChange-Id: {change_id}",
            1_710_000_800,
        )
        rejected = git(self.bob, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("belongs to another principal", rejected.stderr)
        self.assertEqual(
            canonical_before,
            repository_state(self.forge)[0].canonical_tip,
        )

    def test_broken_secondary_does_not_block_source_or_other_operations(self) -> None:
        identity_harness.add_view(
            self.forge,
            "broken",
            ["shared"],
            ["shared"],
            "main",
        )
        broken = forge.view_repo(self.forge, "broken")
        broken.rename(self.root / "broken-disabled.git")

        (self.alice / "shared" / "available.txt").write_text("available\n")
        submitted = commit(self.alice, "available despite broken view", 1_710_000_900)
        git(self.alice, "push", "-q", "origin", "main")
        forge.sync_view(self.forge, "alice")
        self.assertEqual(submitted, self.remote_tip(self.alice_remote))
        forge.sync_view(self.forge, "bob")
        forge.sync_view(self.forge, "broken")
        self.assertTrue(broken.is_symlink())

    def test_disjoint_cross_view_submissions_do_not_need_source_cache_confirmation(self) -> None:
        (self.alice / "team-a" / "overlap.txt").write_text("alice private overlap\n")
        commit(self.alice, "alice private overlap", 1_710_001_000)
        git(self.alice, "push", "-q", "origin", "main")
        self.alice_remote.unlink()

        (self.bob / "shared" / "overlap.txt").write_text("bob shared overlap\n")
        commit(self.bob, "bob shared overlap", 1_710_001_100)
        git(self.bob, "push", "-q", "origin", "main")
        forge.sync_view(self.forge, "alice")
        forge.sync_view(self.forge, "bob")

        manifest, replay = repository_state(self.forge)
        self.assertEqual(manifest.canonical_tip, replay.active_views["alice"]["canonical_frontier"])
        self.assertEqual(manifest.canonical_tip, replay.active_views["bob"]["canonical_frontier"])
        self.assertEqual(
            b"alice private overlap\n",
            forge.git(
                forge.canonical_repo(self.forge),
                "show",
                "main:team-a/overlap.txt",
            ).stdout,
        )
        self.assertEqual(
            b"bob shared overlap\n",
            forge.git(
                forge.canonical_repo(self.forge),
                "show",
                "main:shared/overlap.txt",
            ).stdout,
        )

    def test_committed_request_retry_is_resolved_from_wal(self) -> None:
        from unittest.mock import patch

        old = self.remote_tip(self.alice_remote)
        (self.alice / "shared" / "first-receive.txt").write_text("first\n")
        new = commit(self.alice, "first receive", 1_710_001_200)
        git(self.alice, "push", "-q", "origin", "main")
        head = repository_state(self.forge)[0].head_seq
        update = f"{old} {new} refs/heads/main\n".encode()
        with patch.dict(
            os.environ,
            {
                "BASTION_VIEW": "alice",
                "BASTION_PRINCIPAL_ID": "alice",
                "BASTION_TRANSPORT": "test",
            },
            clear=False,
        ):
            forge.pre_receive(self.forge, "alice", update)
        self.assertEqual(head, repository_state(self.forge)[0].head_seq)

    def test_workers_reconstruct_committed_source_without_local_ref_evidence(self) -> None:
        (self.alice / "shared" / "unconfirmed.txt").write_text("unconfirmed\n")
        new = commit(self.alice, "unconfirmed", 1_710_001_300)
        git(self.alice, "push", "-q", "origin", "main")
        self.alice_remote.unlink()
        self.bob_remote.unlink()

        forge.drain_projection_jobs(self.forge)
        self.assertEqual(new, self.remote_tip(self.alice_remote))
        self.assertEqual(
            b"unconfirmed\n",
            forge.git(self.bob_remote, "show", "main:shared/unconfirmed.txt").stdout,
        )


if __name__ == "__main__":
    unittest.main()
