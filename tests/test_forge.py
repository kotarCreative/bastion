from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import forge
from materialization import ensure_canonical_materialized
from repository import Repository
from tests import identity_harness
from tests.storage_helpers import submission_record
from tests.transport import attach_intake_push




def command(*args: str, cwd: Path | None = None, check: bool = True,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=os.environ | (env or {}))
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def git(repo: Path, *args: str, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return command("git", *args, cwd=repo, check=check, env=env)


def commit(repo: Path, message: str, timestamp: int) -> str:
    env = {
        "GIT_AUTHOR_DATE": f"{timestamp} +0000",
        "GIT_COMMITTER_DATE": f"{timestamp} +0000",
    }
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, env=env)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


class ForgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        git(self.root, "init", "-q", "-b", "main", str(self.source))
        git(self.source, "config", "user.name", "Fixture")
        git(self.source, "config", "user.email", "fixture@example.test")
        (self.source / "visible").mkdir()
        (self.source / "hidden").mkdir()
        (self.source / "visible" / "file.txt").write_text("visible 1\n")
        (self.source / "hidden" / "secret.txt").write_text("secret 1\n")
        commit(self.source, "initial mixed state", 1_700_000_000)
        (self.source / "hidden" / "secret.txt").write_text("secret 2\n")
        commit(self.source, "hidden-only activity", 1_700_000_100)

        self.forge = self.root / "forge"
        forge.init_forge(self.forge, self.source, "main")
        self.admin_principal = identity_harness.provision_admin(self.forge)
        identity_harness.add_view(self.forge, "alice", ["visible"], ["visible"], "main")
        self.remote = forge.view_repo(self.forge, "alice")
        self.alice = self.root / "alice"
        command("git", "clone", "-q", "--no-local", str(self.remote), str(self.alice))
        attach_intake_push(self.forge, self.alice, "alice")
        git(self.alice, "config", "user.name", "Alice")
        git(self.alice, "config", "user.email", "alice@example.test")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def canonical_tip(self) -> str:
        return Repository.open(self.forge).load()[0].canonical_tip

    def remote_tip(self) -> str:
        return forge.ref(self.remote, "refs/heads/main")

    def admin_change(self, path: str, contents: str, message: str, timestamp: int) -> str:
        repository = Repository.open(self.forge)
        manifest, _version, _replay = repository.load()
        ensure_canonical_materialized(self.forge, manifest, repository)
        admin = self.root / f"admin-{timestamp}"
        command("git", "clone", "-q", str(forge.canonical_repo(self.forge)), str(admin))
        identity_harness.attach_admin_push(self.forge, admin)
        git(admin, "config", "user.name", "Admin")
        git(admin, "config", "user.email", "admin@example.test")
        target = admin / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
        oid = commit(admin, message, timestamp)
        git(admin, "push", "-q", "origin", f"{oid}:refs/staging/admin-{timestamp}")
        identity_harness.update_canonical(
            self.forge, manifest.canonical_tip, oid, self.admin_principal
        )
        return oid

    def test_accepted_commits_remain_exact_projected_tip(self) -> None:
        initial = self.remote_tip()
        self.assertEqual(1, len(forge.output(self.remote, "rev-list", "--all").splitlines()))
        (self.alice / "visible" / "file.txt").write_text("alice 1\n")
        change_id = "I1111111111111111111111111111111111111111"
        submitted = commit(self.alice, f"alice change\n\nChange-Id: {change_id}", 1_700_000_200)
        git(self.alice, "push", "-q", "origin", "main")
        forge.sync_view(self.forge, "alice")

        self.assertNotEqual(initial, submitted)
        self.assertEqual(submitted, self.remote_tip())
        self.assertNotEqual(submitted, self.canonical_tip())
        self.assertEqual(submitted, git(self.alice, "rev-parse", "origin/main").stdout.strip())
        mapping = submission_record(
            self.forge, "alice", initial, submitted
        )["payload"]["mappings"][0]
        self.assertEqual(submitted, mapping["submitted"])
        self.assertEqual(self.canonical_tip(), mapping["canonical"])

        (self.alice / "visible" / "second.txt").write_text("second\n")
        commit(self.alice, "first commit in second push", 1_700_000_300)
        (self.alice / "visible" / "third.txt").write_text("third\n")
        second = commit(self.alice, "second commit in second push", 1_700_000_310)
        git(self.alice, "push", "-q", "origin", "main")
        forge.sync_view(self.forge, "alice")
        self.assertEqual(second, self.remote_tip())

    def test_projection_object_database_excludes_hidden_blob(self) -> None:
        hidden = forge.output(
            forge.canonical_repo(self.forge),
            "rev-parse",
            "main:hidden/secret.txt",
        )
        self.assertNotIn(
            hidden,
            forge.output(self.remote, "rev-list", "--objects", "--all").split(),
        )
        self.assertNotEqual(
            0,
            forge.git(self.remote, "cat-file", "-e", hidden, check=False).returncode,
        )
        self.assertFalse((self.remote / "objects" / "info" / "alternates").exists())
        direct_want = git(self.alice, "fetch", "origin", hidden, check=False)
        self.assertNotEqual(0, direct_want.returncode)
        self.assertIn("not our ref", direct_want.stderr)

    def test_hidden_canonical_advancement_does_not_move_projection(self) -> None:
        projected_before = self.remote_tip()
        self.admin_change("hidden/secret.txt", "secret 3\n", "another hidden change", 1_700_000_400)
        self.assertEqual(projected_before, self.remote_tip())

        (self.alice / "visible" / "file.txt").write_text("alice after hidden\n")
        submitted = commit(self.alice, "works over hidden advancement", 1_700_000_500)
        git(self.alice, "push", "-q", "origin", "main")
        forge.sync_view(self.forge, "alice")
        self.assertEqual(submitted, self.remote_tip())
        secret = forge.git(forge.canonical_repo(self.forge), "show", "main:hidden/secret.txt").stdout
        self.assertEqual(b"secret 3\n", secret)

    def test_visible_change_then_revert_still_requires_synchronization(self) -> None:
        (self.alice / "visible" / "pending.txt").write_text("pending\n")
        commit(self.alice, "pending", 1_700_000_510)
        self.admin_change("visible/file.txt", "temporary\n", "temporary visible", 1_700_000_520)
        self.admin_change("visible/file.txt", "visible 1\n", "revert visible", 1_700_000_530)

        rejected = git(self.alice, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("stale or force updates", rejected.stderr)

    def test_visible_advancement_requires_fetch_and_rebase(self) -> None:
        change_id = "I2222222222222222222222222222222222222222"
        (self.alice / "visible" / "alice.txt").write_text("alice\n")
        original = commit(self.alice, f"alice concurrent\n\nChange-Id: {change_id}", 1_700_000_600)
        self.admin_change("visible/admin.txt", "admin\n", "visible admin change", 1_700_000_700)

        rejected = git(self.alice, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("stale or force updates", rejected.stderr)
        forge.sync_view(self.forge, "alice")
        git(self.alice, "fetch", "-q", "origin")
        git(self.alice, "rebase", "origin/main")
        rewritten = git(self.alice, "rev-parse", "HEAD").stdout.strip()
        self.assertNotEqual(original, rewritten)
        git(self.alice, "push", "-q", "origin", "main")
        forge.sync_view(self.forge, "alice")
        self.assertEqual(rewritten, self.remote_tip())
        self.assertIn(f"Change-Id: {change_id}", git(self.alice, "show", "-s", "--format=%B").stdout)
        record = next(
            record
            for record in Repository.open(self.forge).load()[2].records
            if record["kind"] == "submission"
            and any(mapping["change_id"] == change_id for mapping in record["payload"]["mappings"])
        )
        self.assertEqual(rewritten, record["payload"]["mappings"][0]["submitted"])

    def test_unauthorized_second_commit_rejects_entire_push(self) -> None:
        canonical_before = self.canonical_tip()
        projected_before = self.remote_tip()
        (self.alice / "visible" / "allowed.txt").write_text("allowed\n")
        commit(self.alice, "authorized first commit", 1_700_000_800)
        (self.alice / "hidden").mkdir()
        (self.alice / "hidden" / "injected.txt").write_text("injected\n")
        commit(self.alice, "unauthorized second commit", 1_700_000_900)

        rejected = git(self.alice, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("outside current authorization", rejected.stderr)
        self.assertEqual(canonical_before, self.canonical_tip())
        self.assertEqual(projected_before, self.remote_tip())

    def test_other_branch_is_rejected(self) -> None:
        (self.alice / "visible" / "branch.txt").write_text("branch\n")
        commit(self.alice, "other branch", 1_700_001_000)
        rejected = git(self.alice, "push", "origin", "HEAD:refs/heads/other", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("branch update is not authorized", rejected.stderr)
        self.assertEqual(forge.ZERO_OID, forge.ref(self.remote, "refs/heads/other"))

    def test_source_cache_is_reconstructed_from_submission_evidence(self) -> None:
        (self.alice / "visible" / "recover.txt").write_text("recover\n")
        submitted = commit(
            self.alice,
            "recoverable\n\nChange-Id: I3333333333333333333333333333333333333333",
            1_700_001_050,
        )
        git(self.alice, "push", "-q", "origin", "main")
        self.remote.unlink()

        forge.sync_view(self.forge, "alice")

        self.assertEqual(submitted, self.remote_tip())
        record = submission_record(
            self.forge,
            "alice",
            git(self.alice, "rev-parse", "HEAD^").stdout.strip(),
            submitted,
        )
        self.assertEqual(submitted, record["payload"]["projected_new"])

    def test_client_merge_is_rejected(self) -> None:
        git(self.alice, "checkout", "-q", "-b", "side")
        (self.alice / "visible" / "side.txt").write_text("side\n")
        side = commit(self.alice, "side", 1_700_001_100)
        git(self.alice, "checkout", "-q", "main")
        (self.alice / "visible" / "main.txt").write_text("main\n")
        commit(self.alice, "main", 1_700_001_200)
        git(self.alice, "merge", "-q", "--no-ff", "-m", "merge", side)

        rejected = git(self.alice, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("linear chain", rejected.stderr)

    def test_hidden_file_directory_collision_is_rejected(self) -> None:
        identity_harness.add_view(
            self.forge,
            "collision",
            ["container/visible"],
            ["container/visible"],
            "main",
        )
        collision_remote = forge.view_repo(self.forge, "collision")
        collision = self.root / "collision"
        command("git", "clone", "-q", "--no-local", str(collision_remote), str(collision))
        attach_intake_push(self.forge, collision, "collision")
        git(collision, "config", "user.name", "Collision")
        git(collision, "config", "user.email", "collision@example.test")
        canonical = forge.canonical_repo(self.forge)
        admin = self.root / "admin-collision"
        command("git", "clone", "-q", str(canonical), str(admin))
        identity_harness.attach_admin_push(self.forge, admin)
        git(admin, "config", "user.name", "Admin")
        git(admin, "config", "user.email", "admin@example.test")
        (admin / "container").write_text("hidden boundary\n")
        old = self.canonical_tip()
        new = commit(admin, "add hidden ancestor file", 1_700_001_300)
        git(admin, "push", "-q", "origin", f"{new}:refs/staging/collision")
        identity_harness.update_canonical(
            self.forge, old, new, self.admin_principal
        )

        (collision / "container" / "visible").mkdir(parents=True, exist_ok=True)
        (collision / "container" / "visible" / "nested.txt").write_text("nested\n")
        commit(collision, "attempt directory replacement", 1_700_001_400)
        rejected = git(collision, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("file/directory authorization boundary", rejected.stderr)
        self.assertEqual(b"hidden boundary\n", forge.git(canonical, "show", "main:container").stdout)


if __name__ == "__main__":
    unittest.main()
