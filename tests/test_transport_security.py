"""Phase 4 transport boundary: intake isolation, limits, audit, SSH grammar."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import forge
from git import clean_env
from layout import intake_repo, view_repo
from tests import identity_harness
from tests.transport import LoopbackTransport


REPO_ROOT = Path(__file__).resolve().parent.parent
SSH_SHIM = REPO_ROOT / "tools" / "bastion-ssh"


def command(*args: str, cwd: Path | None = None, check: bool = True,
            env: dict | None = None) -> subprocess.CompletedProcess[str]:
    merged = os.environ | {"GIT_TERMINAL_PROMPT": "0"} | (env or {})
    result = subprocess.run(args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=merged)
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def commit(repo: Path, message: str, timestamp: int) -> str:
    env = {
        "GIT_AUTHOR_DATE": f"{timestamp} +0000",
        "GIT_COMMITTER_DATE": f"{timestamp} +0000",
    }
    command("git", "-C", str(repo), "add", "-A")
    command("git", "-C", str(repo), "commit", "-q", "-m", message,
            env=os.environ | env)
    return command("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()


class TransportFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.forge = self.root / "forge"
        source = self.root / "source"
        command("git", "init", "-q", "-b", "main", str(source))
        command("git", "-C", str(source), "config", "user.name", "Fixture")
        command("git", "-C", str(source), "config", "user.email",
                "fixture@example.test")
        (source / "shared").mkdir()
        (source / "team-a").mkdir()
        (source / "hidden").mkdir()
        (source / "shared" / "base.txt").write_text("base\n")
        (source / "team-a" / "a.txt").write_text("a\n")
        (source / "hidden" / "secret.txt").write_text("secret\n")
        commit(source, "initial", 1_700_000_000)
        forge.init_forge(self.forge, source, "main")
        identity_harness.provision_admin(self.forge)
        identity_harness.add_view(self.forge, "alice",
                                  ["shared", "team-a"], ["shared", "team-a"], "main")
        identity_harness.add_view(self.forge, "bob", ["shared"], ["shared"], "main")
        self.transport = LoopbackTransport(self.forge)

    def clone(self, view: str, name: str) -> Path:
        checkout = self.root / name
        command("git", "clone", "-q", "--no-local", self.transport.url(view),
                str(checkout))
        command("git", "-C", str(checkout), "config", "user.name", name.title())
        command("git", "-C", str(checkout), "config", "user.email",
                f"{name}@example.test")
        return checkout

    def close(self) -> None:
        self.transport.stop()
        self.temporary.cleanup()


class IntakeIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.alice = self.fixture.clone("alice", "alice-work")

    def test_rejected_push_leaves_view_store_byte_identical(self) -> None:
        import hashlib

        def digest() -> str:
            h = hashlib.sha256()
            base = view_repo(self.fixture.forge, "alice") / "objects"
            for path in sorted(base.rglob("*")):
                if path.is_file():
                    h.update(str(path.relative_to(base)).encode())
                    h.update(path.read_bytes())
            return h.hexdigest()

        before = digest()
        command("git", "-C", str(self.alice), "checkout", "-qb", "bad")
        (self.alice / "shared" / "x.txt").write_text("x\n")
        commit(self.alice, "unauthorized branch", 1_700_000_100)
        pushed = command("git", "-C", str(self.alice), "push", "origin", "bad",
                         check=False)
        self.assertNotEqual(0, pushed.returncode)
        self.assertIn("[policy]", pushed.stderr)
        self.assertEqual(before, digest())

    def test_direct_view_push_is_refused_with_stable_message(self) -> None:
        (self.alice / "shared" / "y.txt").write_text("y\n")
        commit(self.alice, "direct attempt", 1_700_000_200)
        pushed = command(
            "git", "-C", str(self.alice), "push",
            str(view_repo(self.fixture.forge, "alice")), "main", check=False,
        )
        self.assertNotEqual(0, pushed.returncode)
        self.assertIn(
            "pushes are accepted only through the authenticated intake boundary",
            pushed.stderr,
        )

    def test_intake_namespace_never_resolves_hidden_objects(self) -> None:
        # The canonical tip carries hidden-only history invisible to alice.
        hidden_tip = forge.output(forge.canonical_repo(self.fixture.forge),
                                  "rev-parse", "refs/heads/main")
        probe = forge.git(intake_repo(self.fixture.forge, "alice"),
                          "cat-file", "-t", hidden_tip, check=False)
        self.assertNotEqual(0, probe.returncode)

    def test_intake_resolves_bases_living_only_in_the_view_store(self) -> None:
        """Alternates make view-only projection objects usable as bases."""
        bob = self.fixture.clone("bob", "bob-work")
        (bob / "shared" / "fanout.txt").write_text("fanout\n")
        bob_new = commit(bob, "fanout trigger", 1_700_100_000)
        command("git", "-C", str(bob), "push", "-q", "origin", "main")
        synthetic = forge.ref(view_repo(self.fixture.forge, "alice"),
                              "refs/heads/main")
        self.assertNotIn(synthetic, ("", "0" * 40))
        resolved = forge.git(intake_repo(self.fixture.forge, "alice"),
                             "cat-file", "-t", synthetic).stdout.decode().strip()
        self.assertEqual("commit", resolved)
        # Alice's next push still works against the mirrored advertisement.
        (self.alice / "team-a" / "after-fanout.txt").write_text("next\n")
        alice_new = commit(self.alice, "after fanout", 1_700_100_200)
        command("git", "-C", str(self.alice), "pull", "-q", "--rebase", "origin",
                "main")
        del bob_new, alice_new
        command("git", "-C", str(self.alice), "push", "-q", "origin", "main")


class PushLimitsTest(unittest.TestCase):
    LIMIT_KEYS = ("BASTION_MAX_PUSH_COMMITS", "BASTION_MAX_PUSH_OBJECTS",
                  "BASTION_MAX_OBJECT_BYTES")

    def setUp(self) -> None:
        self._saved = {key: os.environ.get(key) for key in self.LIMIT_KEYS}
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.alice = self.fixture.clone("alice", "alice-work")

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _push_two_commits(self) -> subprocess.CompletedProcess[str]:
        (self.alice / "shared" / "limit-a.txt").write_text("a\n")
        commit(self.alice, "limit one", 1_700_200_000)
        (self.alice / "shared" / "limit-b.txt").write_text("b\n")
        commit(self.alice, "limit two", 1_700_200_100)
        return command("git", "-C", str(self.alice), "push", "origin", "main",
                       check=False)

    def test_depth_limit_is_enforced_with_stable_category(self) -> None:
        # Limits are read inside the receive hook process, so they travel
        # through the environment the server hands to its children.
        os.environ["BASTION_MAX_PUSH_COMMITS"] = "1"
        pushed = self._push_two_commits()
        self.assertNotEqual(0, pushed.returncode)
        self.assertIn("[limits]", pushed.stderr)

    def test_object_count_limit_is_enforced_with_stable_category(self) -> None:
        os.environ["BASTION_MAX_PUSH_OBJECTS"] = "1"
        pushed = self._push_two_commits()
        self.assertNotEqual(0, pushed.returncode)
        self.assertIn("[limits]", pushed.stderr)

    def test_oversized_object_is_rejected_with_stable_category(self) -> None:
        os.environ["BASTION_MAX_OBJECT_BYTES"] = "8"
        big = b"x" * 4096
        for index in range(4):
            (self.alice / "shared" / f"big{index}.bin").write_bytes(big)
            commit(self.alice, f"big {index}", 1_700_201_000 + index)
        pushed = command("git", "-C", str(self.alice), "push", "-q", "origin",
                         "main", check=False)
        self.assertNotEqual(0, pushed.returncode)
        self.assertIn("[limits]", pushed.stderr)


class AuditIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        from store import open_store

        self.open_store = open_store
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.alice = self.fixture.clone("alice", "alice-work")

    def _entries(self, kind: str) -> list[dict]:
        store = self.open_store(self.fixture.forge)
        try:
            return [e for e in store.recent_audit(50) if e["kind"] == kind]
        finally:
            store.close()

    def test_accepted_and_rejected_receives_are_audited_with_identity(self) -> None:
        (self.alice / "shared" / "audit.txt").write_text("audit\n")
        commit(self.alice, "audited change", 1_700_300_000)
        command("git", "-C", str(self.alice), "push", "-q", "origin", "main")
        accepted = self._entries("receive.accept")
        self.assertEqual(1, len(accepted))
        self.assertIn("alice/alice", accepted[0]["subject"])
        self.assertIn("http", accepted[0]["detail"])

        command("git", "-C", str(self.alice), "checkout", "-qb", "nope")
        (self.alice / "shared" / "bad.txt").write_text("bad\n")
        commit(self.alice, "rejected branch", 1_700_300_100)
        command("git", "-C", str(self.alice), "push", "-q", "origin", "nope",
                check=False)
        rejected = self._entries("receive.reject")
        self.assertEqual(1, len(rejected))
        self.assertIn("policy", rejected[0]["detail"])
        self.assertIn("http", rejected[0]["detail"])

    def test_token_minting_is_audited(self) -> None:
        self.fixture.transport.token("alice")
        self.assertEqual(1, len(self._entries("token.issue")))


class SshShimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = TransportFixture()
        self.addCleanup(self.fixture.close)
        self.backend = f"http://127.0.0.1:{self.fixture.transport.port}"
        self.token = self.fixture.transport.token("alice")

    def _ssh_command(self, user: str = "alice", token: str | None = None) -> str:
        return (
            f"{sys.executable} {SSH_SHIM} --backend {self.backend}"
            f" --token {token or self.token}"
            f" --ssh-user {user}"
        )

    def _git(self, *args: str, ssh: str, check: bool = True):
        env = os.environ | {
            "GIT_SSH_COMMAND": ssh,
            "GIT_SSH_VARIANT": "ssh",
            "GIT_PROTOCOL": "version=2",
            "GIT_TERMINAL_PROMPT": "0",
        }
        result = subprocess.run(
            ["git", "-c", "protocol.version=2", *args], text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
        if check and result.returncode:
            raise AssertionError(f"git {' '.join(args)}\n{result.stdout}\n{result.stderr}")
        return result

    def _exchange(self, arguments: list[str], payload: bytes,
                  token: str | None = None, user: str = "alice"):
        """One deterministic shim invocation: full stdin, full stdout."""
        return subprocess.run(
            [sys.executable, str(SSH_SHIM), "--backend", self.backend,
             "--token", token or self.token, "--ssh-user", user,
             *arguments],
            input=payload, capture_output=True,
            env=os.environ | {"GIT_PROTOCOL": "version=2"},
        )

    @staticmethod
    def _pkt(payload: bytes) -> bytes:
        return f"{len(payload) + 4:04x}".encode() + payload

    def test_fetch_round_trip_over_ssh_grammar(self) -> None:
        tip = forge.ref(view_repo(self.fixture.forge, "alice"),
                        "refs/heads/main")
        ls_refs = (
            self._pkt(b"command=ls-refs")
            + b"0001"
            + self._pkt(b"peel")
            + self._pkt(b"symrefs")
            + self._pkt(b"ref-prefix refs/heads/")
            + b"0000"
        )
        fetch = (
            self._pkt(b"command=fetch")
            + b"0001"
            + self._pkt(b"want " + tip.encode())
            + self._pkt(b"done")
            + b"0000"
        )
        result = self._exchange(
            ["alice@127.0.0.1", "git-upload-pack '/alice.git'"],
            ls_refs + fetch,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        # One connection serves both exchanges back to back.
        self.assertIn(b"refs/heads/main", result.stdout)
        self.assertIn(b"PACK", result.stdout)

    def test_push_round_trip_over_ssh_grammar(self) -> None:
        client = self.fixture.clone("alice", "ssh-push-work")
        old = command("git", "-C", str(client), "rev-parse", "HEAD").stdout.strip()
        (client / "shared" / "via-ssh.txt").write_text("pushed through shim\n")
        new = commit(client, "push over ssh shim", 1_700_500_000)
        pack = subprocess.run(
            ["git", "-C", str(client), "pack-objects", "--revs", "--stdout"],
            input=f"{new}\n^{old}\n".encode(), capture_output=True, check=True,
        ).stdout
        commands = (
            self._pkt(f"{old} {new} refs/heads/main\0report-status".encode())
            + b"0000"
        )
        result = self._exchange(
            ["alice@127.0.0.1", "git-receive-pack '/alice.git'"],
            commands + pack,
        )
        self.assertEqual(0, result.returncode, result.stderr.decode())
        self.assertIn(b"ok refs/heads/main", result.stdout)
        view_tip = forge.ref(view_repo(self.fixture.forge, "alice"),
                             "refs/heads/main")
        self.assertEqual(new, view_tip)
        canonical_tip = forge.ref(forge.canonical_repo(self.fixture.forge),
                                  "refs/heads/main")
        self.assertNotEqual(canonical_tip, view_tip)
        intake_tip = forge.git(intake_repo(self.fixture.forge, "alice"),
                               "rev-parse", "refs/heads/main").stdout.decode().strip()
        self.assertEqual(new, intake_tip)

    def test_identity_mismatch_between_username_and_path_is_refused(self) -> None:
        bob_token = self.fixture.transport.token("bob")
        url = f"ssh://bob@127.0.0.1:{self.fixture.transport.port}/alice.git"
        destination = self.fixture.root / "ssh-wrong-user"
        result = self._git("clone", "-q", "--no-local", url, str(destination),
                           ssh=self._ssh_command(user="bob", token=bob_token),
                           check=False)
        self.assertNotEqual(0, result.returncode)
        self.assertIn("does not match", result.stderr)

    def test_non_git_commands_exit_127(self) -> None:
        shim = subprocess.run(
            [sys.executable, str(SSH_SHIM), "--backend", self.backend,
             "--token", self.token, "--ssh-user", "alice",
             "alice@127.0.0.1", "rm -rf /"],
            input=b"", capture_output=True,
        )
        self.assertEqual(127, shim.returncode)

    def test_traversal_paths_are_refused(self) -> None:
        shim = subprocess.run(
            [sys.executable, str(SSH_SHIM), "--backend", self.backend,
             "--token", self.token, "--ssh-user", "alice",
             "alice@127.0.0.1", "git-upload-pack '/../canonical.git'"],
            input=b"", capture_output=True,
        )
        self.assertNotEqual(0, shim.returncode)


if __name__ == "__main__":
    unittest.main()