from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import forge
from tests import identity_harness
from errors import ForgeError
from git import ref
from http_server import add_token, make_server
from invariants import check_invariants
from layout import ZERO_OID, canonical_repo
from tests.storage_helpers import document_bytes, read_document, repository_state




def command(*args: str, cwd: Path | None = None, check: bool = True,
            env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ | {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "true",
    } | (env or {})
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=merged)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def git(repo: Path, *args: str, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return command("git", *args, cwd=repo, check=check, env=env)


class HttpForgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(self.source))
        git(self.source, "config", "user.name", "Fixture")
        git(self.source, "config", "user.email", "fixture@example.test")
        (self.source / "visible").mkdir()
        (self.source / "hidden").mkdir()
        (self.source / "visible" / "file.txt").write_text("visible 1\n")
        (self.source / "hidden" / "secret.txt").write_text("secret 1\n")
        environment = dict(
            os.environ,
            GIT_AUTHOR_DATE="1700000000 +0000",
            GIT_COMMITTER_DATE="1700000000 +0000",
        )
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "initial mixed state", env=environment)

        self.forge = self.root / "forge"
        forge.init_forge(self.forge, self.source, "main")
        identity_harness.add_view(self.forge, "alice", ["visible"], ["visible"], "main")
        identity_harness.add_view(self.forge, "bob", ["visible"], ["visible"], "main")
        self.initial_canonical = ref(canonical_repo(self.forge), "refs/heads/main")

        self.alice_token = add_token(
            self.forge, "alice", scopes=["fetch", "push", "ui"]
        )
        self.bob_token = add_token(
            self.forge, "bob", scopes=["fetch", "push", "ui"]
        )
        self.server = make_server(self.forge, "127.0.0.1", 0)
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=10)
        self.temporary.cleanup()

    def url(self, view: str, token: str | None = None) -> str:
        if token is None:
            return f"http://127.0.0.1:{self.port}/{view}.git"
        return f"http://{view}:{token}@127.0.0.1:{self.port}/{view}.git"

    def clone(self, view: str, destination: Path, token: str | None = None,
              *extra: str) -> Path:
        command("git", "clone", "-q", "--no-local", *extra,
                self.url(view, token), str(destination))
        git(destination, "config", "user.name", view.title())
        git(destination, "config", "user.email", f"{view}@example.test")
        return destination

    def test_clone_push_fanout_and_fetch(self) -> None:
        alice = self.clone("alice", self.root / "alice-work", self.alice_token)
        (alice / "visible" / "http.txt").write_text("over http\n")
        git(alice, "add", "-A")
        git(alice, "commit", "-q", "-m", "pushed over http")
        git(alice, "push", "-q", "origin", "main")

        canonical_tip = repository_state(self.forge)[0].canonical_tip
        self.assertNotEqual(canonical_tip, self.initial_canonical)
        check_invariants(self.forge)

        bob = self.clone("bob", self.root / "bob-work", self.bob_token)
        self.assertEqual((bob / "visible" / "http.txt").read_text(), "over http\n")
        self.assertNotIn("hidden", os.listdir(bob))
        check_invariants(self.forge)

        git(alice, "fetch", "-q", "origin")
        git(alice, "rev-parse", "--verify", "origin/main")

    def test_bearer_token_authentication(self) -> None:
        result = command(
            "git", "-c", f"http.extraHeader=Authorization: Bearer {self.alice_token}",
            "ls-remote", self.url("alice"), "refs/heads/main",
        )
        self.assertIn("refs/heads/main", result.stdout)

    def test_protocol_v2_clone(self) -> None:
        destination = self.root / "v2-clone"
        command("git", "-c", "protocol.version=2", "clone", "-q", "--no-local",
                self.url("alice", self.alice_token), str(destination))
        git(destination, "rev-parse", "--verify", "HEAD")

    def test_shallow_clone(self) -> None:
        destination = self.root / "shallow"
        self.clone("alice", destination, self.alice_token, "--depth", "1")
        count = git(destination, "rev-list", "--count", "HEAD").stdout.strip()
        self.assertEqual(count, "1")

    def test_push_rejected_over_http_keeps_state(self) -> None:
        alice = self.clone("alice", self.root / "alice-reject", self.alice_token)
        before_state = document_bytes(self.forge)
        before_canonical = ref(canonical_repo(self.forge), "refs/heads/main")
        git(alice, "checkout", "-q", "-b", "feature")
        (alice / "visible" / "other.txt").write_text("wrong branch\n")
        git(alice, "add", "-A")
        git(alice, "commit", "-q", "-m", "unauthorized branch")
        result = git(alice, "push", "origin", "feature", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(document_bytes(self.forge), before_state)
        self.assertEqual(ref(canonical_repo(self.forge), "refs/heads/main"), before_canonical)
        check_invariants(self.forge)


if __name__ == "__main__":
    unittest.main()
