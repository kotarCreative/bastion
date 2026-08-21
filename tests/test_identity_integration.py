from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.identity_harness import ENTRYPOINT
from tests.identity_harness import add_view as fixture_add_view

import forge
from service import (
    add_principal,
    add_role,
    add_service_account,
    add_team,
    bind_role,
    sync_view,
)


def command(*args: str, cwd: Path | None = None, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ | (env or {}))
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def git(repo: Path, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    return command("git", *args, cwd=repo, check=check, env=env)


def commit(repo: Path, message: str, timestamp: int) -> str:
    env = {
        "GIT_AUTHOR_DATE": f"{timestamp} +0000",
        "GIT_COMMITTER_DATE": f"{timestamp} +0000",
    }
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, env=env)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


class IdentityIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        git(self.root, "init", "-q", "-b", "main", str(source))
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        for name in ("shared", "team-a", "team-b"):
            directory = source / name
            directory.mkdir()
            (directory / "base.txt").write_text(f"{name} base\n")
        commit(source, "initial", 1_700_000_000)

        self.forge = self.root / "forge"
        forge.init_forge(self.forge, source, "main")
        # alice-primary/alice-secondary are two endpoints of one principal.
        add_principal(self.forge, "alice")
        add_principal(self.forge, "bob")
        add_role(self.forge, "shared-write", ["shared"], ["shared"])
        bind_role(self.forge, "shared-write", "principal", "alice")
        bind_role(self.forge, "shared-write", "principal", "bob")
        add_team(self.forge, "engineering", ["bob"])
        add_role(self.forge, "team-a-grant", ["team-a"], ["team-a"])
        bind_role(self.forge, "team-a-grant", "team", "engineering")
        fixture_add_view(self.forge, "alice-primary", ["shared"], ["shared"], "main", principal="alice")
        fixture_add_view(self.forge, "alice-secondary", ["shared"], ["shared"], "main", principal="alice")
        fixture_add_view(self.forge, "bob", ["shared", "team-b"], ["shared", "team-b"], "main")
        self.clients: dict[str, Path] = {}
        for name in ("alice-primary", "alice-secondary", "bob"):
            checkout = self.root / name
            command("git", "clone", "-q", "--no-local", str(forge.view_repo(self.forge, name)), str(checkout))
            git(checkout, "config", "user.name", name.title())
            git(checkout, "config", "user.email", f"{name}@example.test")
            self.clients[name] = checkout

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def state(self) -> dict:
        return json.loads((self.forge / "state.json").read_text())

    def descriptor_of(self, view: str) -> dict:
        state = self.state()
        return state["view_descriptors"][state["views"][view]["descriptor_id"]]

    def test_view_name_and_principal_are_recorded_separately(self) -> None:
        transaction_state = self.state()
        self.assertEqual(
            transaction_state["views"]["alice-primary"]["principal_id"], "alice"
        )
        self.assertEqual(
            transaction_state["views"]["alice-secondary"]["principal_id"], "alice"
        )

    def test_two_views_share_one_descriptor_and_fanout_excludes_only_source(self) -> None:
        self.assertEqual(
            self.descriptor_of("alice-primary")["id"],
            self.descriptor_of("alice-secondary")["id"],
        )
        primary = self.clients["alice-primary"]
        (primary / "shared" / "change.txt").write_text("change\n")
        submitted = commit(primary, "primary change\n\nChange-Id: I" + "a" * 40, 1_700_000_100)
        git(primary, "push", "-q", "origin", "main")
        forge.drain_projection_jobs(self.forge)

        state = self.state()
        transaction = next(iter(state["transactions"].values()))
        self.assertEqual("alice", transaction["principal_id"])
        self.assertEqual(
            sorted(transaction["target_descriptors"]), ["alice-secondary", "bob"]
        )
        secondary_tip = forge.ref(forge.view_repo(self.forge, "alice-secondary"), "refs/heads/main")
        self.assertNotEqual(secondary_tip, forge.ZERO_OID)
        synthetic = state["views"]["alice-secondary"]["anchors"][secondary_tip]
        self.assertEqual("synthetic", synthetic["kind"])
        self.assertEqual(
            transaction["descriptor_id"], synthetic["descriptor_id"]
        )

    def test_same_principal_continues_change_id_from_other_view(self) -> None:
        change_id = "I" + "b" * 40
        primary = self.clients["alice-primary"]
        (primary / "shared" / "first.txt").write_text("first\n")
        commit(primary, f"first\n\nChange-Id: {change_id}", 1_700_000_200)
        git(primary, "push", "-q", "origin", "main")
        forge.drain_projection_jobs(self.forge)

        secondary = self.clients["alice-secondary"]
        command("git", "fetch", "-q", "origin", cwd=secondary)
        command("git", "reset", "-q", "--hard", "origin/main", cwd=secondary)
        (secondary / "shared" / "second.txt").write_text("second\n")
        commit(secondary, f"second\n\nChange-Id: {change_id}", 1_700_000_300)
        git(secondary, "push", "-q", "origin", "main")
        forge.drain_projection_jobs(self.forge)
        change = self.state()["changes"][change_id]
        self.assertEqual("alice", change["owner_principal_id"])
        submitted_views = {
            revision["view"]
            for revision in change["revisions"]
            if revision["kind"] == "submitted"
        }
        self.assertEqual({"alice-primary", "alice-secondary"}, submitted_views)

    def test_other_principal_with_equal_roles_cannot_claim_change_id(self) -> None:
        change_id = "I" + "c" * 40
        primary = self.clients["alice-primary"]
        (primary / "shared" / "owned.txt").write_text("owned\n")
        commit(primary, f"owned\n\nChange-Id: {change_id}", 1_700_000_400)
        git(primary, "push", "-q", "origin", "main")
        forge.drain_projection_jobs(self.forge)

        bob = self.clients["bob"]
        command("git", "fetch", "-q", "origin", cwd=bob)
        command("git", "reset", "-q", "--hard", "origin/main", cwd=bob)
        (bob / "shared" / "stolen.txt").write_text("stolen\n")
        commit(bob, f"stolen\n\nChange-Id: {change_id}", 1_700_000_500)
        rejected = git(bob, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("belongs to another principal", rejected.stderr)

    def test_service_account_pushes_and_owns_its_changes(self) -> None:
        add_service_account(self.forge, "release-bot", "alice")
        add_role(self.forge, "bot-shared", ["shared"], ["shared"])
        bind_role(self.forge, "bot-shared", "principal", "release-bot")
        fixture_add_view(self.forge, "release-bot", ["shared"], ["shared"], "main")
        bot = self.root / "client-release-bot"
        command("git", "clone", "-q", "--no-local", str(forge.view_repo(self.forge, "release-bot")), str(bot))
        git(bot, "config", "user.name", "Release Bot")
        git(bot, "config", "user.email", "bot@example.test")

        change_id = "I" + "d" * 40
        (bot / "shared" / "bot.txt").write_text("built\n")
        commit(bot, f"built\n\nChange-Id: {change_id}", 1_700_000_600)
        git(bot, "push", "-q", "origin", "main")
        forge.drain_projection_jobs(self.forge)

        change = self.state()["changes"][change_id]
        self.assertEqual("release-bot", change["owner_principal_id"])

        # The human owner gains no claim through ownership of the account.
        primary = self.clients["alice-primary"]
        command("git", "fetch", "-q", "origin", cwd=primary)
        command("git", "reset", "-q", "--hard", "origin/main", cwd=primary)
        (primary / "shared" / "hijack.txt").write_text("hijack\n")
        commit(primary, f"hijack\n\nChange-Id: {change_id}", 1_700_000_700)
        rejected = git(primary, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("belongs to another principal", rejected.stderr)

    def test_team_grants_shape_the_projected_tree(self) -> None:
        bob = self.clients["bob"]
        # team-a is visible only through the engineering team binding.
        expected = {
            "shared/base.txt": b"shared base\n",
            "team-a/base.txt": b"team-a base\n",
            "team-b/base.txt": b"team-b base\n",
        }
        listing = sorted(
            command("git", "ls-tree", "-r", "--name-only", "HEAD", cwd=bob).stdout.splitlines()
        )
        self.assertEqual(sorted(expected), listing)

        admin = self.root / "admin"
        command("git", "clone", "-q", str(forge.canonical_repo(self.forge)), str(admin))
        git(admin, "config", "user.name", "Admin")
        git(admin, "config", "user.email", "admin@example.test")
        target = admin / "team-a"
        target.mkdir(parents=True, exist_ok=True)
        (target / "new.txt").write_text("team a new\n")
        new = commit(admin, "team a via team grant", 1_700_000_800)
        old = command("git", "rev-parse", "HEAD~1", cwd=admin).stdout.strip()
        command("git", "push", "-q", "origin", f"{new}:refs/staging/team-test", cwd=admin)
        forge.update_canonical_ref(self.forge, old, new)
        forge.drain_projection_jobs(self.forge)

        command("git", "fetch", "-q", "origin", cwd=bob)
        bob_tip = forge.ref(forge.view_repo(self.forge, "bob"), "refs/heads/main")
        anchor = self.state()["views"]["bob"]["anchors"][bob_tip]
        self.assertEqual(new, anchor["canonical"])
        self.assertEqual(anchor["descriptor_id"], self.descriptor_of("bob")["id"])
        listing = command("git", "ls-tree", "-r", "--name-only", bob_tip, cwd=bob).stdout.splitlines()
        self.assertIn("team-a/new.txt", listing)

    def test_read_only_descriptor_rejects_pushes_without_moving_refs(self) -> None:
        add_principal(self.forge, "reader")
        add_role(self.forge, "read-shared", ["shared"], [])
        bind_role(self.forge, "read-shared", "principal", "reader")
        fixture_add_view(self.forge, "reader", ["shared"], [], "main")
        reader = self.root / "client-reader"
        command("git", "clone", "-q", "--no-local", str(forge.view_repo(self.forge, "reader")), str(reader))
        git(reader, "config", "user.name", "Reader")
        git(reader, "config", "user.email", "reader@example.test")

        before_refs = forge.ref(forge.canonical_repo(self.forge), "refs/heads/main")
        before_state = (self.forge / "state.json").read_bytes()
        (reader / "shared" / "write.txt").write_text("unauthorized\n")
        commit(reader, "reader write attempt", 1_700_000_900)
        rejected = git(reader, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("outside current authorization", rejected.stderr)
        self.assertEqual(before_refs, forge.ref(forge.canonical_repo(self.forge), "refs/heads/main"))
        self.assertEqual(before_state, (self.forge / "state.json").read_bytes())

    def test_environment_cannot_override_the_state_selected_identity(self) -> None:
        bob = self.clients["bob"]
        command("git", "fetch", "-q", "origin", cwd=bob)
        command("git", "reset", "-q", "--hard", "origin/main", cwd=bob)
        (bob / "shared" / "env.txt").write_text("env\n")
        new = commit(bob, "env push\n\nChange-Id: I" + "e" * 40, 1_700_001_000)
        old = command("git", "rev-parse", "HEAD~1", cwd=bob).stdout.strip()
        forge.run("git", "--git-dir", str(forge.view_repo(self.forge, "bob")), "fetch", "-q", str(bob), new)

        hostile_env = os.environ.copy()
        hostile_env.update(
            {
                "BASTION_PRINCIPAL": "alice",
                "PRINCIPAL": "alice",
                "BASTION_DESCRIPTOR": "vdesc-forged",
                "BASTION_VIEW": "alice-primary",
                "GIT_PUSH_OPTION_PRINCIPAL": "alice",
            }
        )
        updates = f"{old} {new} refs/heads/main\n".encode()
        previous_umask = os.umask(0o022)
        try:
            forge.pre_receive(self.forge, "bob", updates)
        finally:
            os.umask(previous_umask)
        forge.git(forge.view_repo(self.forge, "bob"), "update-ref", "refs/heads/main", new, old)
        forge.post_receive(self.forge, "bob", updates)

        state = self.state()
        transaction = next(tx for tx in state["transactions"].values() if tx.get("view") == "bob")
        self.assertEqual("bob", transaction["principal_id"])
        self.assertEqual(state["views"]["bob"]["descriptor_id"], transaction["descriptor_id"])
        self.assertNotIn("bob", transaction["target_descriptors"])
        self.assertEqual(
            {"alice-primary", "alice-secondary"},
            set(transaction["target_descriptors"]),
        )


if __name__ == "__main__":
    unittest.main()
