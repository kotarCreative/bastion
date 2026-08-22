from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import forge
from tests import identity_harness
from errors import ForgeError
from tests.storage_helpers import document_bytes, read_document




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
        git(checkout, "config", "user.name", name.title())
        git(checkout, "config", "user.email", f"{name}@example.test")
        return checkout

    def state(self) -> dict:
        return read_document(self.forge)

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

        state = self.state()
        shared_revisions = state["changes"][shared_change]["revisions"]
        self.assertTrue(
            any(
                revision["view"] == "alice"
                and revision["projected"] == alice_shared
                and revision["kind"] == "submitted"
                for revision in shared_revisions
            )
        )
        self.assertTrue(
            any(
                revision["view"] == "bob"
                and revision["projected"] == bob_after_alice
                and revision["kind"] == "synthetic"
                for revision in shared_revisions
            )
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
        self.assertEqual(alice_concurrent, self.remote_tip(self.alice_remote))
        rejected = git(self.bob, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("fetch first", rejected.stderr)

        git(self.bob, "fetch", "-q", "origin")
        git(self.bob, "rebase", "origin/main")
        bob_rebased = git(self.bob, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(bob_before_rebase, bob_rebased)
        git(self.bob, "push", "-q", "origin", "main")
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

    def test_recovery_completes_pending_secondary_view(self) -> None:
        bob_old = self.remote_tip(self.bob_remote)
        change_id = "Idddddddddddddddddddddddddddddddddddddddd"
        (self.alice / "shared" / "recover.txt").write_text("recover cross view\n")
        submitted = commit(
            self.alice,
            f"cross-view recovery\n\nChange-Id: {change_id}",
            1_710_000_600,
        )
        git(self.alice, "push", "-q", "origin", "main")
        bob_new = self.remote_tip(self.bob_remote)
        state = self.state()
        transaction = next(iter(state["transactions"].values()))
        bob_anchor = state["views"]["bob"]["anchors"].pop(bob_new)
        state["views"]["bob"]["projected_tip"] = bob_old
        state["views"]["bob"]["canonical_frontier"] = transaction["canonical_old"]
        state["changes"][change_id]["revisions"] = [
            revision
            for revision in state["changes"][change_id]["revisions"]
            if revision["view"] != "bob"
        ]
        transaction["status"] = "fanout_pending"
        transaction["pending_views"] = ["bob"]
        job = next(
            job
            for job in state["projection_jobs"].values()
            if job["transaction_id"] == transaction["id"] and job["view"] == "bob"
        )
        job["status"] = "retry"
        job["next_attempt_at"] = 0
        forge.save_state(self.forge, state)

        forge.drain_projection_jobs(self.forge)
        recovered = self.state()
        recovered_bob = self.remote_tip(self.bob_remote)
        self.assertEqual(bob_new, recovered_bob)
        self.assertEqual("published", recovered["transactions"][transaction["id"]]["status"])
        self.assertEqual(
            bob_anchor["canonical"],
            recovered["views"]["bob"]["anchors"][recovered_bob]["canonical"],
        )
        self.assertTrue(
            any(
                revision["view"] == "bob"
                and revision["projected"] == recovered_bob
                and revision["kind"] == "synthetic"
                for revision in recovered["changes"][change_id]["revisions"]
            )
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
        canonical_before = forge.ref(
            forge.canonical_repo(self.forge), "refs/heads/main"
        )

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
            forge.ref(forge.canonical_repo(self.forge), "refs/heads/main"),
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
        self.assertEqual(submitted, self.remote_tip(self.alice_remote))
        state = self.state()
        transaction = next(iter(state["transactions"].values()))
        self.assertEqual("fanout_pending", transaction["status"])
        self.assertIn("broken", transaction["pending_views"])
        self.assertIn("broken", transaction["view_errors"])
        forge.sync_view(self.forge, "bob")

    def test_unconfirmed_source_serializes_cross_view_transactions(self) -> None:
        alice_old = self.remote_tip(self.alice_remote)
        bob_old = self.remote_tip(self.bob_remote)
        (self.alice / "team-a" / "overlap.txt").write_text("alice private overlap\n")
        alice_new = commit(self.alice, "alice private overlap", 1_710_001_000)
        forge.run(
            "git",
            "--git-dir",
            str(self.alice_remote),
            "fetch",
            "-q",
            str(self.alice),
            alice_new,
        )
        forge.pre_receive(
            self.forge,
            "alice",
            f"{alice_old} {alice_new} refs/heads/main\n".encode(),
        )

        (self.bob / "shared" / "overlap.txt").write_text("bob shared overlap\n")
        bob_new = commit(self.bob, "bob shared overlap", 1_710_001_100)
        forge.run(
            "git",
            "--git-dir",
            str(self.bob_remote),
            "fetch",
            "-q",
            str(self.bob),
            bob_new,
        )
        bob_update = f"{bob_old} {bob_new} refs/heads/main\n".encode()
        with self.assertRaisesRegex(ForgeError, "unconfirmed source publication"):
            forge.pre_receive(self.forge, "bob", bob_update)
        forge.git(
            self.alice_remote,
            "update-ref",
            "refs/heads/main",
            alice_new,
            alice_old,
        )
        forge.post_receive(
            self.forge,
            "alice",
            f"{alice_old} {alice_new} refs/heads/main\n".encode(),
        )
        forge.pre_receive(self.forge, "bob", bob_update)
        forge.git(
            self.bob_remote,
            "update-ref",
            "refs/heads/main",
            bob_new,
            bob_old,
        )
        forge.post_receive(self.forge, "bob", bob_update)
        forge.drain_projection_jobs(self.forge)
        forge.sync_view(self.forge, "bob")

        state = self.state()
        canonical_tip = forge.ref(
            forge.canonical_repo(self.forge), "refs/heads/main"
        )
        self.assertEqual(canonical_tip, state["views"]["alice"]["canonical_frontier"])
        self.assertEqual(canonical_tip, state["views"]["bob"]["canonical_frontier"])
        self.assertEqual(
            self.remote_tip(self.alice_remote), state["views"]["alice"]["projected_tip"]
        )
        self.assertEqual(
            self.remote_tip(self.bob_remote), state["views"]["bob"]["projected_tip"]
        )
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
        alice_count = int(
            forge.output(self.alice_remote, "rev-list", "--count", "main")
        )
        bob_count = int(forge.output(self.bob_remote, "rev-list", "--count", "main"))
        self.assertEqual(3, alice_count)
        self.assertEqual(2, bob_count)

    def test_second_receive_on_same_view_is_rejected_until_source_publishes(self) -> None:
        old = self.remote_tip(self.alice_remote)
        (self.alice / "shared" / "first-receive.txt").write_text("first\n")
        new = commit(self.alice, "first receive", 1_710_001_200)
        forge.run(
            "git",
            "--git-dir",
            str(self.alice_remote),
            "fetch",
            "-q",
            str(self.alice),
            new,
        )
        update = f"{old} {new} refs/heads/main\n".encode()
        forge.pre_receive(self.forge, "alice", update)
        with self.assertRaisesRegex(ForgeError, "active receive transaction"):
            forge.pre_receive(self.forge, "alice", update)

    def test_unconfirmed_source_is_not_published_by_recovery(self) -> None:
        old = self.remote_tip(self.alice_remote)
        (self.alice / "shared" / "unconfirmed.txt").write_text("unconfirmed\n")
        new = commit(self.alice, "unconfirmed", 1_710_001_300)
        forge.run(
            "git",
            "--git-dir",
            str(self.alice_remote),
            "fetch",
            "-q",
            str(self.alice),
            new,
        )
        forge.pre_receive(
            self.forge,
            "alice",
            f"{old} {new} refs/heads/main\n".encode(),
        )
        transaction = next(iter(self.state()["transactions"].values()))
        (self.forge / "receivers" / transaction["id"]).unlink(missing_ok=True)
        forge.drain_projection_jobs(self.forge)
        recovered = self.state()["transactions"][transaction["id"]]
        self.assertEqual(old, self.remote_tip(self.alice_remote))
        self.assertEqual("source_unconfirmed", recovered["status"])
        self.assertEqual("blocked", self.state()["outbox"][recovered["event_id"]]["status"])
        with self.assertRaisesRegex(ForgeError, "unconfirmed source publication"):
            forge.sync_view(self.forge, "bob")
        bob_old = self.remote_tip(self.bob_remote)
        (self.bob / "shared" / "barrier.txt").write_text("barrier\n")
        bob_new = commit(self.bob, "cannot cross unconfirmed barrier", 1_710_001_350)
        rejected = git(self.bob, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("unconfirmed source publication", rejected.stderr)
        self.assertEqual(bob_old, self.remote_tip(self.bob_remote))

        update = f"{old} {new} refs/heads/main\n".encode()
        forge.pre_receive(self.forge, "alice", update)
        forge.git(
            self.alice_remote,
            "update-ref",
            "refs/heads/main",
            new,
            old,
        )
        forge.post_receive(self.forge, "alice", update)
        retried = self.state()["transactions"][transaction["id"]]
        self.assertEqual(new, self.remote_tip(self.alice_remote))
        self.assertEqual("published", retried["status"])
        self.assertEqual(
            1,
            int(
                forge.output(
                    forge.canonical_repo(self.forge),
                    "rev-list",
                    "--count",
                    f"{transaction['canonical_old']}..main",
                )
            ),
        )


if __name__ == "__main__":
    unittest.main()
