from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import forge
from errors import ForgeError
from identity import normalize_emails
from invariants import check_invariants
from service import (
    add_principal,
    add_role,
    add_service_account,
    bind_role,
)
from tests.identity_harness import (
    ENTRYPOINT,
    capability_grants,
    provision_admin,
)
from service import add_view as service_add_view


def git(repo: Path, *args: str, check: bool = True, env: dict | None = None):
    return command("git", *args, cwd=repo, check=check, env=env)


def command(*args: str, cwd: Path | None = None, check: bool = True, env: dict | None = None):
    result = subprocess.run(
        args, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**__import__("os").environ, **(env or {})},
    )
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def commit(repo: Path, message: str, timestamp: int, author_email: str | None = None) -> str:
    env = {
        "GIT_AUTHOR_DATE": f"{timestamp} +0000",
        "GIT_COMMITTER_DATE": f"{timestamp} +0000",
    }
    if author_email is not None:
        env["GIT_AUTHOR_EMAIL"] = author_email
    git(repo, "add", "-A")
    command("git", "commit", "-q", "-m", message, cwd=repo, env=env)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


class MetadataFixture:
    """alice and bob each declare exactly one author address."""

    def __init__(self, root: Path) -> None:
        self.root = root
        source = root / "source"
        command("git", "init", "-q", "-b", "main", str(source), cwd=root)
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.test")
        shared = source / "shared"
        shared.mkdir(parents=True)
        (shared / "base.txt").write_text("base\n")
        commit(source, "initial", 1_700_000_000)

        self.forge = root / "forge"
        forge.init_forge(self.forge, source, "main")
        provision_admin(self.forge)
        add_principal(self.forge, "alice", ["alice@example.test"])
        add_principal(self.forge, "bob", ["bob@example.test"])
        allow, deny = capability_grants(["shared"], ["shared"])
        add_role(self.forge, "writer", allow, deny)
        bind_role(self.forge, "writer", "principal", "alice")
        bind_role(self.forge, "writer", "principal", "bob")
        service_add_view(self.forge, "alice", "alice", "main", ENTRYPOINT)

        self.client = root / "client-alice"
        command("git", "clone", "-q", "--no-local",
                str(forge.view_repo(self.forge, "alice")), str(self.client))
        git(self.client, "config", "user.name", "Alice Local")
        git(self.client, "config", "user.email", "alice@example.test")

    def state(self) -> dict:
        return json.loads((self.forge / "state.json").read_text())

    def push(self, message: str = "change\n", timestamp: int = 1_700_100_000,
             author_email: str | None = None) -> subprocess.CompletedProcess[str]:
        commit(self.client, f"{message}\n\nChange-Id: I{'a' * 40}", timestamp,
               author_email)
        return git(self.client, "push", "-q", "origin", "main", check=False)


class AuthorBindingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = MetadataFixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_declared_author_is_accepted(self) -> None:
        (self.fixture.client / "shared" / "new.txt").write_text("new\n")
        result = self.fixture.push(author_email="alice@example.test")
        self.assertEqual(0, result.returncode, result.stderr)
        forge.drain_projection_jobs(self.fixture.forge)
        check_invariants(self.fixture.forge)

    def test_foreign_author_is_rejected_without_state_change(self) -> None:
        # Bob's identity may not act through Alice's view.
        (self.fixture.client / "shared" / "new.txt").write_text("new\n")
        before = self.fixture.state()
        rejected = self.fixture.push(author_email="bob@example.test")
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("undeclared identity", rejected.stderr)
        self.assertEqual(before["views"], self.fixture.state()["views"])
        view_state = before["views"]["alice"]
        actual = forge.ref(forge.view_repo(self.fixture.forge, "alice"),
                           view_state["projected_ref"])
        self.assertEqual(view_state["projected_tip"], actual)

    def test_unknown_author_is_rejected_for_bound_principals(self) -> None:
        (self.fixture.client / "shared" / "new.txt").write_text("new\n")
        rejected = self.fixture.push(author_email="root@example.test")
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("undeclared identity", rejected.stderr)

    def test_unrestricted_principals_accept_any_author(self) -> None:
        add_principal(self.fixture.forge, "carol")  # no declared emails
        allow, deny = capability_grants(["shared"], ["shared"])
        add_role(self.fixture.forge, "writer-2", allow, deny)
        bind_role(self.fixture.forge, "writer-2", "principal", "carol")
        service_add_view(self.fixture.forge, "carol", "carol", "main", ENTRYPOINT)
        carol = self.fixture.root / "client-carol"
        command("git", "clone", "-q", "--no-local",
                str(forge.view_repo(self.fixture.forge, "carol")), str(carol))
        git(carol, "config", "user.name", "Anyone")
        git(carol, "config", "user.email", "anyone@elsewhere.test")
        (carol / "shared" / "c.txt").write_text("c\n")
        commit(carol, "legacy\n\nChange-Id: I" + "b" * 40, 1_700_200_000)
        git(carol, "push", "-q", "origin", "main")
        forge.drain_projection_jobs(self.fixture.forge)
        check_invariants(self.fixture.forge)

    def test_service_accounts_own_their_addresses(self) -> None:
        add_service_account(self.fixture.forge, "bot", "alice", ["bot@example.test"])
        allow, deny = capability_grants(["shared"], ["shared"])
        add_role(self.fixture.forge, "bot-writer", allow, deny)
        bind_role(self.fixture.forge, "bot-writer", "principal", "bot")
        service_add_view(self.fixture.forge, "bot", "bot", "main", ENTRYPOINT)
        bot = self.fixture.root / "client-bot"
        command("git", "clone", "-q", "--no-local",
                str(forge.view_repo(self.fixture.forge, "bot")), str(bot))
        git(bot, "config", "user.name", "Bot")
        git(bot, "config", "user.email", "bot@example.test")
        (bot / "shared" / "b.txt").write_text("b\n")
        commit(bot, "build\n\nChange-Id: I" + "c" * 40, 1_700_300_000)
        git(bot, "push", "-q", "origin", "main")
        # The owner's address does not authorize the service account.
        (bot / "shared" / "b2.txt").write_text("b2\n")
        commit(bot, "hijack\n\nChange-Id: I" + "d" * 40, 1_700_400_000,
               author_email="alice@example.test")
        rejected = git(bot, "push", "origin", "main", check=False)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("undeclared identity", rejected.stderr)


class TimestampBoundTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.fixture = MetadataFixture(Path(self.temporary.name))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_future_dated_commit_is_rejected(self) -> None:
        (self.fixture.client / "shared" / "future.txt").write_text("future\n")
        rejected = self.fixture.push(timestamp=4_100_000_000)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("future-dated", rejected.stderr)

    def test_backdated_history_remains_accepted(self) -> None:
        (self.fixture.client / "shared" / "old.txt").write_text("old\n")
        result = self.fixture.push(timestamp=100_000_000)
        self.assertEqual(0, result.returncode, result.stderr)
        forge.drain_projection_jobs(self.fixture.forge)
        check_invariants(self.fixture.forge)


class EmailAdmissionTest(unittest.TestCase):
    def test_emails_are_normalized_sorted_and_unique(self) -> None:
        self.assertEqual(
            ["a@example.test", "b@example.test"],
            normalize_emails(["B@example.test", "a@example.test", "A@Example.Test"]),
        )

    def test_invalid_addresses_are_rejected(self) -> None:
        from identity import add_principal as catalog_add_principal

        state = {
            "principals": {},
            "roles": {},
            "role_bindings": {},
            "teams": {},
            "views": {},
            "view_descriptors": {},
        }
        for bad in ("no-at-sign", "two@@at.test", "spaces in@test.example", ""):
            with self.subTest(bad=bad), self.assertRaises(ForgeError):
                catalog_add_principal(state, "p", [bad])

    def test_emails_are_immutable_after_creation(self) -> None:
        from service import _catalog_operation  # noqa: F401

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            command("git", "init", "-q", "-b", "main", str(source), cwd=root)
            git(source, "config", "user.name", "F")
            git(source, "config", "user.email", "f@e.t")
            (source / "f.txt").write_text("f\n")
            commit(source, "i", 1_700_000_000)
            forge_root = root / "forge"
            forge.init_forge(forge_root, source, "main")
            add_principal(forge_root, "dave", ["dave@example.test"])
            state = json.loads((forge_root / "state.json").read_text())
            self.assertEqual(
                ["dave@example.test"],
                state["principals"]["dave"]["emails"],
            )
            with self.assertRaisesRegex(ForgeError, "already exists"):
                add_principal(forge_root, "dave", ["other@example.test"])


class TagCapabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(source), cwd=self.root)
        git(source, "config", "user.name", "F")
        git(source, "config", "user.email", "f@e.t")
        (source / "s.txt").write_text("s\n")
        commit(source, "i", 1_700_000_000)
        self.forge = self.root / "forge"
        forge.init_forge(self.forge, source, "main")
        add_principal(self.forge, "alice")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_tag_grants_resolve_into_descriptors_but_stay_dormant(self) -> None:
        allow, deny = capability_grants(["shared"], [])
        allow["tag"] = [""]
        add_role(self.forge, "tagger", allow, deny)
        bind_role(self.forge, "tagger", "principal", "alice")
        service_add_view(self.forge, "alice", "alice", "main", ENTRYPOINT)
        state = json.loads((self.forge / "state.json").read_text())
        descriptor = state["view_descriptors"][state["views"]["alice"]["descriptor_id"]]
        self.assertEqual([""], descriptor["capabilities"]["tag"])
        self.assertEqual([], descriptor["restrictions"]["tag"])
        # Tag objects remain unsupported; only the projected branch exists.
        repo = forge.view_repo(self.forge, "alice")
        listing = forge.output(repo, "for-each-ref", "refs/tags/").strip()
        self.assertEqual("", listing)
        check_invariants(self.forge)


if __name__ == "__main__":
    unittest.main()
