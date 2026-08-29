from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import forge
from errors import ForgeError
from service import (


    add_principal,
    add_role,
    add_team,
    bind_role,
    transition_principal,
)
from tests.identity_harness import (
    attach_admin_push,
    capability_grants,
    provision_admin,
    update_canonical,
)
from service import add_view as service_add_view
from tests.storage_helpers import repository_state, wal_records
from tests.transport import attach_intake_push


EMPTY_DENY = {key: [] for key in (
    "read", "create", "modify", "delete", "mode", "symlink",
    "branch", "merge", "admin", "tag",
)}


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return command("git", *args, cwd=repo, check=check)


def command(*args: str, cwd: Path | None = None, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    import os

    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ | (env or {}))
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def commit(repo: Path, message: str, timestamp: int) -> str:
    env = {
        "GIT_AUTHOR_DATE": f"{timestamp} +0000",
        "GIT_COMMITTER_DATE": f"{timestamp} +0000",
    }
    git(repo, "add", "-A")
    command("git", "commit", "-q", "-m", message, cwd=repo, env=env)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


class CapabilityFixture:
    """One principal with a developer role over shared plus two views."""

    def __init__(self, root: Path) -> None:
        self.root = root
        source = root / "source"
        command("git", "init", "-q", "-b", "main", str(source), cwd=root)
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        for name in ("shared", "shared/private", "team-b"):
            directory = source / name
            directory.mkdir(parents=True)
            (directory / "base.txt").write_text(f"{name}\n")
        commit(source, "initial", 1_700_000_000)

        self.forge = root / "forge"
        forge.init_forge(self.forge, source, "main")
        provision_admin(self.forge)
        add_principal(self.forge, "alice")

        allow = {key: [] for key in EMPTY_DENY}
        allow["read"] = ["shared"]
        # Deliberately no modify/delete/mode/symlink: effects are separated.
        allow["create"] = ["shared"]
        add_role(self.forge, "developer", allow, EMPTY_DENY)
        bind_role(self.forge, "developer", "principal", "alice")
        service_add_view(self.forge, "alice", "alice", "main")

        self.client = root / "client"
        command("git", "clone", "-q", "--no-local", str(forge.view_repo(self.forge, "alice")), str(self.client))
        attach_intake_push(self.forge, self.client, "alice")
        git(self.client, "config", "user.name", "Alice")
        git(self.client, "config", "user.email", "alice@example.test")

    def state(self) -> dict:
        manifest, replay = repository_state(self.forge)
        return {
            "views": {
                name: {
                    **view,
                    "canonical_ref": manifest.canonical_ref,
                    "projected_tip": forge.ref(
                        forge.view_repo(self.forge, name), view["projected_ref"]
                    ),
                }
                for name, view in replay.active_views.items()
            }
        }

    def push(self, change_id: str, timestamp: int = 1_700_100_000) -> subprocess.CompletedProcess[str]:
        commit(self.client, f"change\n\nChange-Id: {change_id}", timestamp)
        return git(self.client, "push", "-q", "origin", "main", check=False)

    def tip_listing(self) -> list[str]:
        return command(
            "git", "ls-tree", "-r", "--name-only", "HEAD", cwd=self.client
        ).stdout.splitlines()


class CapabilityEffectTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = CapabilityFixture(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def refs_unchanged(self, view: str = "alice") -> None:
        view_state = self.fixture.state()["views"][view]
        actual = forge.ref(forge.view_repo(self.fixture.forge, view), view_state["projected_ref"])
        self.assertEqual(view_state["projected_tip"], actual)

    def test_create_within_capability_is_accepted(self) -> None:
        (self.fixture.client / "shared" / "new.txt").write_text("new\n")
        result = self.fixture.push("I" + "a" * 40)
        self.assertEqual(0, result.returncode, result.stderr)
        forge.drain_projection_jobs(self.fixture.forge)

    def test_modify_without_modify_capability_is_rejected(self) -> None:
        # The fixture role grants create but not modify.
        (self.fixture.client / "shared" / "base.txt").write_text("changed\n")
        before = self.fixture.state()
        rejected = self.fixture.push("I" + "b" * 40)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("outside current authorization", rejected.stderr)
        self.assertEqual(before["views"], self.fixture.state()["views"])
        self.refs_unchanged()

    def test_delete_requires_the_delete_capability(self) -> None:
        (self.fixture.client / "shared" / "base.txt").unlink()
        rejected = self.fixture.push("I" + "c" * 40)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("outside current authorization", rejected.stderr)
        self.refs_unchanged()
    def test_mode_change_requires_the_mode_capability(self) -> None:
        script = self.fixture.client / "shared" / "new.sh"
        script.write_text("#!/bin/sh\n")
        self.fixture.push("I" + "d" * 40)
        forge.drain_projection_jobs(self.fixture.forge)
        command("git", "fetch", "-q", "origin", cwd=self.fixture.client)
        command("git", "reset", "-q", "--hard", "origin/main", cwd=self.fixture.client)
        script.chmod(0o755)
        commit(self.fixture.client, "mode flip\n\nChange-Id: I" + "e" * 40, 1_700_200_000)
        rejected = git(self.fixture.client, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("outside current authorization", rejected.stderr)
        self.refs_unchanged()

    def test_symlink_creation_requires_the_symlink_capability(self) -> None:
        link = self.fixture.client / "shared" / "link"
        command("ln", "-s", "/etc/hostname", str(link), cwd=self.fixture.root)
        git(self.fixture.client, "add", "-A")
        commit(self.fixture.client, "link\n\nChange-Id: I" + "f" * 40, 1_700_300_000)
        rejected = git(self.fixture.client, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("outside current authorization", rejected.stderr)
        self.refs_unchanged()


class DenyRuleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = CapabilityFixture(self.root)
        # A more specific deny hides shared/private from alice's projection.
        allow, deny = capability_grants(["unrelated"], [])
        deny["read"] = ["shared/private"]
        add_role(self.fixture.forge, "hider", allow, deny)
        bind_role(self.fixture.forge, "hider", "principal", "alice")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_deny_hides_paths_until_transition(self) -> None:
        # Dormant until the explicit policy transition applies it.
        listing_before = self.fixture.tip_listing()
        transition_principal(self.fixture.forge, "alice")
        forge.drain_projection_jobs(self.fixture.forge)
        command("git", "fetch", "-q", "origin", cwd=self.fixture.client)
        command("git", "reset", "-q", "--hard", "origin/main", cwd=self.fixture.client)
        listing_after = self.fixture.tip_listing()
        self.assertIn("shared/private/base.txt", listing_before)
        self.assertNotIn("shared/private/base.txt", listing_after)

    def test_contraction_rejects_pushes_touching_hidden_paths(self) -> None:
        transition_principal(self.fixture.forge, "alice")
        forge.drain_projection_jobs(self.fixture.forge)
        command("git", "fetch", "-q", "origin", cwd=self.fixture.client)
        command("git", "reset", "-q", "--hard", "origin/main", cwd=self.fixture.client)
        hidden = self.fixture.client / "shared" / "private"
        hidden.mkdir(parents=True, exist_ok=True)
        (hidden / "attempt.txt").write_text("sneak\n")
        commit(self.fixture.client, "smuggle\n\nChange-Id: I" + "1" * 40, 1_700_400_000)
        rejected = git(self.fixture.client, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("outside current authorization", rejected.stderr)

    def test_policy_transition_isolates_previously_projected_history(self) -> None:
        old_tip = forge.ref(
            forge.view_repo(self.fixture.forge, "alice"), "refs/heads/main"
        )
        transition_principal(self.fixture.forge, "alice")
        repo = forge.view_repo(self.fixture.forge, "alice")
        result = forge.run(
            "git",
            "--git-dir",
            str(repo),
            "cat-file",
            "-e",
            f"{old_tip}^{{commit}}",
            check=False,
        )
        self.assertNotEqual(0, result.returncode)
        self.assertEqual(
            1, len(forge.output(repo, "rev-list", "refs/heads/main").splitlines())
        )


class TransitionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = CapabilityFixture(self.root)
        # Expansion: team-b becomes readable through a new team binding.
        add_team(self.fixture.forge, "team-b", ["alice"])
        allow, _deny = capability_grants(["team-b"], [])
        add_role(self.fixture.forge, "team-b-reader", allow, EMPTY_DENY)
        bind_role(self.fixture.forge, "team-b-reader", "team", "team-b")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_expansion_starts_a_parentless_projection_epoch(self) -> None:
        state = self.fixture.state()
        view = state["views"]["alice"]
        old_tip = view["projected_tip"]
        old_generation = view["descriptor_generation"]
        old_epoch = view["projection_epoch"]
        old_listing = command(
            "git", "--git-dir", str(forge.view_repo(self.fixture.forge, "alice")),
            "ls-tree", "-r", "--name-only", old_tip,
        ).stdout.splitlines()

        transition_principal(self.fixture.forge, "alice")

        state = self.fixture.state()
        view = state["views"]["alice"]
        self.assertNotEqual(old_tip, view["descriptor_id"] and view["projected_tip"])
        self.assertEqual(old_generation + 1, view["descriptor_generation"])
        self.assertEqual(old_epoch + 1, view["projection_epoch"])
        repo = forge.view_repo(self.fixture.forge, "alice")
        commit_line = forge.output(
            repo, "rev-list", "--parents", "-n", "1", view["projected_tip"]
        ).split()
        self.assertEqual([view["projected_tip"]], commit_line)
        self.assertNotEqual(
            0,
            forge.git(repo, "cat-file", "-e", old_tip, check=False).returncode,
        )

        listing = command(
            "git",
            "--git-dir",
            str(forge.view_repo(self.fixture.forge, "alice")),
            "ls-tree",
            "-r",
            "--name-only",
            view["projected_tip"],
        ).stdout.splitlines()
        self.assertNotIn("team-b/base.txt", old_listing)
        self.assertIn("team-b/base.txt", listing)

        from invariants import check_invariants

        check_invariants(self.fixture.forge)

    def test_second_transition_without_changes_is_a_silent_noop(self) -> None:
        transition_principal(self.fixture.forge, "alice")
        before = repository_state(self.fixture.forge)[0].head_seq
        transition_principal(self.fixture.forge, "alice")
        self.assertEqual(before, repository_state(self.fixture.forge)[0].head_seq)

    def test_unknown_or_viewless_principals_are_rejected(self) -> None:
        with self.assertRaisesRegex(ForgeError, "unknown principal"):
            transition_principal(self.fixture.forge, "ghost")
        add_principal(self.fixture.forge, "lonely")
        with self.assertRaisesRegex(ForgeError, "no views"):
            transition_principal(self.fixture.forge, "lonely")

    def test_transition_orders_after_unmaterialized_canonical_work(self) -> None:
        admin_checkout = self.fixture.root / "admin"
        command("git", "clone", "-q", str(forge.canonical_repo(self.fixture.forge)), str(admin_checkout))
        attach_admin_push(self.fixture.forge, admin_checkout)
        git(admin_checkout, "config", "user.name", "Admin")
        git(admin_checkout, "config", "user.email", "admin@example.test")
        (admin_checkout / "late.txt").write_text("late\n")
        new = commit(admin_checkout, "late admin change", 1_700_500_000)
        old = git(admin_checkout, "rev-parse", "HEAD~1").stdout.strip()
        command("git", "push", "-q", "origin", f"{new}:refs/staging/transition-late", cwd=admin_checkout)
        update_canonical(self.fixture.forge, old, new)
        transition_principal(self.fixture.forge, "alice")
        _manifest, replay = repository_state(self.fixture.forge)
        self.assertEqual("policy_transition", replay.records[-1]["kind"])
        self.assertEqual(new, replay.active_views["alice"]["canonical_frontier"])


class AdminCapabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(source), cwd=self.root)
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        for name in ("shared", "team-a"):
            directory = source / name
            directory.mkdir(parents=True)
            (directory / "base.txt").write_text(f"{name}\n")
        commit(source, "initial", 1_700_000_000)
        self.forge = self.root / "forge"
        forge.init_forge(self.forge, source, "main")
        add_principal(self.forge, "scoped-admin")
        allow = {key: [] for key in EMPTY_DENY}
        allow["admin"] = ["team-a"]
        add_role(self.forge, "team-a-admin", allow, EMPTY_DENY)
        bind_role(self.forge, "team-a-admin", "principal", "scoped-admin")
        self.checkout = self.root / "admin"
        command("git", "clone", "-q", str(forge.canonical_repo(self.forge)), str(self.checkout))
        attach_admin_push(self.forge, self.checkout)
        git(self.checkout, "config", "user.name", "Admin")
        git(self.checkout, "config", "user.email", "admin@example.test")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _stage(self, path: str, contents: str) -> tuple[str, str]:
        target = self.checkout / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents)
        new = commit(self.checkout, f"admin {path}", 1_700_600_000)
        old = git(self.checkout, "rev-parse", "HEAD~1").stdout.strip()
        command(
            "git",
            "push",
            "-q",
            "origin",
            f"{new}:refs/staging/admin-capability",
            cwd=self.checkout,
        )
        return old, new

    def test_update_outside_admin_coverage_is_rejected_in_place(self) -> None:
        old, new = self._stage("shared/escalation.txt", "denied\n")
        with self.assertRaisesRegex(ForgeError, "lacks admin coverage"):
            update_canonical(self.forge, old, new, "scoped-admin")
        manifest, replay = repository_state(self.forge)
        self.assertEqual(old, manifest.canonical_tip)
        self.assertFalse(any(record["kind"] == "canonical_update" for record in replay.records))

    def test_update_within_coverage_is_recorded_with_the_actor(self) -> None:
        old, new = self._stage("team-a/approved.txt", "allowed\n")
        update_canonical(self.forge, old, new, "scoped-admin")
        record = wal_records(self.forge, "canonical_update")[-1]
        transaction = record["payload"]
        self.assertEqual("scoped-admin", transaction["principal_id"])
        self.assertIn("descriptor_id", transaction)
        self.assertEqual(transaction["descriptor_id"], record["event_fingerprint"]["descriptor_id"])
        self.assertIn("received_at", transaction)
        self.assertEqual(new, repository_state(self.forge)[0].canonical_tip)


if __name__ == "__main__":
    unittest.main()
