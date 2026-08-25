"""Disposable view and intake materialization safety."""

from __future__ import annotations

import json
import shlex
import subprocess
import time
import unittest

import forge
from errors import ForgeError
from layout import intake_repo, view_repo
from materialization import REJECT_ALL_HOOK, ensure_intake_materialized, ensure_view_materialized
from repository import Repository
from tests.storage_helpers import repository_state
from tests.tree_harness import TreeFixture, clean_environment, command
from wal import canonical_json


class MaterializationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TreeFixture()
        self.addCleanup(self.fixture.close)

    def test_sync_repairs_an_existing_non_executable_intake_hook(self) -> None:
        forge.sync_view(self.fixture.root, "alice")
        hook = intake_repo(self.fixture.root, "alice").resolve() / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o644)

        forge.sync_view(self.fixture.root, "alice")

        self.assertEqual(0o755, hook.stat().st_mode & 0o777)
        self.assertIn("forge.py", hook.read_text())
        before = repository_state(self.fixture.root)[0].head_seq
        _old, new = self.fixture.source_commit(
            "alice", {b"shared/hook.txt": (b"100644", b"hook repaired\n")}
        )
        command("git", "-C", self.fixture.clients["alice"], "push", "-q", "origin", "main")
        manifest, replay = repository_state(self.fixture.root)
        self.assertEqual(before + 1, manifest.head_seq)
        self.assertEqual(new, replay.records[-1]["payload"]["projected_new"])

    def test_sync_rebuilds_a_self_consistent_but_false_view_tip(self) -> None:
        self.fixture.push_source(
            "alice", {b"shared/cursor.txt": (b"100644", b"authoritative\n")}
        )
        manifest, replay = repository_state(self.fixture.root)
        active = replay.active_views["alice"]
        link = view_repo(self.fixture.root, "alice")
        generation = link.resolve()
        projected_ref = active["projected_ref"]
        authoritative = command(
            "git", "--git-dir", generation, "rev-parse", projected_ref
        ).stdout.decode().strip()
        parent = command(
            "git", "--git-dir", generation, "rev-parse", f"{authoritative}^"
        ).stdout.decode().strip()
        command("git", "--git-dir", generation, "update-ref", projected_ref, parent)
        cursor_path = generation / "bastion-cursor.json"
        cursor = json.loads(cursor_path.read_bytes())
        cursor["projected_tip"] = parent
        cursor_path.write_bytes(canonical_json(cursor))

        forge.sync_view(self.fixture.root, "alice")

        self.assertNotEqual(generation, link.resolve())
        self.assertEqual(
            authoritative,
            command("git", "--git-dir", link, "rev-parse", projected_ref)
            .stdout.decode()
            .strip(),
        )
        forge.check_invariants(self.fixture.root)

    def test_sync_repairs_the_projected_view_reject_hook(self) -> None:
        forge.sync_view(self.fixture.root, "alice")
        link = view_repo(self.fixture.root, "alice")
        generation = link.resolve()
        hook = generation / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o644)
        command(
            "git", "--git-dir", generation, "config", "extensions.worktreeConfig", "true"
        )
        command(
            "git", "--git-dir", generation, "config", "--worktree", "core.hooksPath", "bypass-hooks"
        )

        forge.sync_view(self.fixture.root, "alice")

        repaired = link.resolve() / "hooks" / "pre-receive"
        self.assertEqual(REJECT_ALL_HOOK, repaired.read_text())
        self.assertEqual(0o755, repaired.stat().st_mode & 0o777)
        self.assertEqual(
            "hooks",
            command(
                "git", "--git-dir", link, "config", "--local", "core.hooksPath"
            ).stdout.decode().strip(),
        )
        self.assertNotEqual(generation, link.resolve())

    def test_sync_rebuilds_an_intake_with_redirected_hooks(self) -> None:
        forge.sync_view(self.fixture.root, "alice")
        link = intake_repo(self.fixture.root, "alice")
        generation = link.resolve()
        command(
            "git", "--git-dir", generation, "config", "core.hooksPath", "bypass-hooks"
        )

        forge.sync_view(self.fixture.root, "alice")

        self.assertNotEqual(generation, link.resolve())
        self.assertEqual(
            "hooks",
            command(
                "git", "--git-dir", link, "config", "--local", "core.hooksPath"
            ).stdout.decode().strip(),
        )

    def test_new_intake_generation_preserves_active_receives_and_rejects_stale_work(self) -> None:
        repository = Repository.open(self.fixture.root)
        manifest, _version, replay = repository.load()
        old_view = ensure_view_materialized(
            self.fixture.root, "alice", manifest, replay, repository
        )
        intake_link = intake_repo(self.fixture.root, "alice")
        old_intake = intake_link.resolve()

        self.fixture.push_source(
            "alice", {b"shared/intake.txt": (b"100644", b"new generation\n")}
        )

        new_intake = intake_link.resolve()
        self.assertNotEqual(old_intake, new_intake)
        self.assertTrue(old_intake.is_dir())
        with self.assertRaisesRegex(ForgeError, "superseded"):
            ensure_intake_materialized(self.fixture.root, old_view)
        self.assertEqual(new_intake, intake_link.resolve())

    def test_receive_finishes_in_a_superseded_intake_generation(self) -> None:
        forge.sync_view(self.fixture.root, "alice")
        intake_link = intake_repo(self.fixture.root, "alice")
        old_intake = intake_link.resolve()
        hooks = old_intake / "hooks"
        hook = hooks / "pre-receive"
        real_hook = hooks / "pre-receive.real"
        marker = self.fixture.directory / "receive-started"
        release = self.fixture.directory / "release-receive"
        captured = self.fixture.directory / "receiving-intake"
        real_contents = hook.read_text().replace(
            "export BASTION_INTAKE_REPO\n",
            "export BASTION_INTAKE_REPO\n"
            f"printf '%s\\n' \"$BASTION_INTAKE_REPO\" > {shlex.quote(str(captured))}\n",
        )
        real_hook.write_text(real_contents)
        real_hook.chmod(0o755)
        hook.write_text(
            "#!/bin/sh\n"
            f"touch {shlex.quote(str(marker))}\n"
            f"while [ ! -e {shlex.quote(str(release))} ]; do sleep 0.01; done\n"
            f"exec {shlex.quote(str(real_hook))}\n"
        )
        hook.chmod(0o755)
        _old, new = self.fixture.source_commit(
            "alice", {b"shared/concurrent.txt": (b"100644", b"active receive\n")}
        )
        process = subprocess.Popen(
            ["git", "-C", str(self.fixture.clients["alice"]), "push", "-q", "origin", "main"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=clean_environment(),
        )
        try:
            deadline = time.monotonic() + 10
            while not marker.exists() and time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                time.sleep(0.01)
            self.assertTrue(marker.exists(), "receive hook did not start")
            self.fixture.admin_update(
                [{b"hidden/concurrent.txt": (b"100644", b"hidden advancement\n")}]
            )
            self.assertNotEqual(old_intake, intake_link.resolve())
            self.assertTrue(old_intake.is_dir())
            release.touch()
            _stdout, stderr = process.communicate(timeout=30)
        finally:
            release.touch(exist_ok=True)
            if process.poll() is None:
                process.kill()
                process.communicate()

        self.assertEqual(0, process.returncode, stderr.decode(errors="replace"))
        self.assertEqual(str(old_intake), captured.read_text().strip())
        _manifest, replay = repository_state(self.fixture.root)
        self.assertEqual(new, replay.records[-1]["payload"]["projected_new"])


if __name__ == "__main__":
    unittest.main()
