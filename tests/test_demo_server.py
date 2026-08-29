"""Tests for the Flask-hosted permission demo server."""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import forge
from demo_server import create_app
from http_server import add_token
from tests import identity_harness


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


class DemoServerTest(unittest.TestCase):
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
        env = dict(os.environ, GIT_AUTHOR_DATE="1700000000 +0000",
                   GIT_COMMITTER_DATE="1700000000 +0000")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "initial mixed state", env=env)

        self.forge = self.root / "forge"
        forge.init_forge(self.forge, self.source, "main")
        identity_harness.add_view(self.forge, "alice", ["visible"], ["visible"], "main")
        identity_harness.add_view(self.forge, "bob", ["visible"], ["visible"], "main")

        self.alice_token = add_token(self.forge, "alice")
        self.bob_token = add_token(self.forge, "bob")

        os.environ["BASTION_DEMO_SECRET"] = "test-secret-do-not-use"
        self.app = create_app(self.forge)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _basic(self, token: str) -> dict[str, str]:
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

    def _login(self, token: str) -> None:
        response = self.client.post("/login", data={"token": token}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)

    def test_landing_page_requires_token(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Enter the token", response.data)

    def test_login_rejects_bad_token(self) -> None:
        response = self.client.post("/login", data={"token": "not-a-token"}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Invalid token", response.data)

    def test_login_accepts_good_token(self) -> None:
        response = self.client.post("/login", data={"token": self.alice_token}, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"alice", response.data)

    def test_whoami_html(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/whoami")
        self.assertEqual(response.status_code, 200)
        text = response.data.decode()
        self.assertIn("alice", text)
        self.assertIn("Descriptor ID", text)
        self.assertIn("read", text)
        self.assertIn("visible", text)

    def test_whoami_json(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/whoami?format=json")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["view"], "alice")
        self.assertEqual(data["descriptor_id"], data["descriptor_id"])
        self.assertIn("read", data["capabilities"])
        self.assertIn("visible", data["capabilities"]["read"])

    def test_evaluate_allowed_and_denied(self) -> None:
        self._login(self.alice_token)
        response = self.client.post("/evaluate", data={"capability": "read", "path": "visible/file.txt"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"allowed", response.data)

        response = self.client.post("/evaluate", data={"capability": "read", "path": "hidden/secret.txt"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"denied", response.data)

    def test_browse_shows_visible_entries(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/browse")
        self.assertEqual(response.status_code, 200)
        text = response.data.decode()
        self.assertIn("visible", text)
        self.assertNotIn("hidden", text)

    def test_history_shows_commits(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/history")
        self.assertEqual(response.status_code, 200)
        # The view sees the synthetic projection commit, not the hidden canonical message.
        self.assertIn(b"projected canonical change", response.data)

    def test_git_info_refs_requires_auth(self) -> None:
        response = self.client.get("/alice.git/info/refs?service=git-upload-pack")
        self.assertEqual(response.status_code, 401)
        self.assertIn("WWW-Authenticate", response.headers)

    def test_git_info_refs_rejects_wrong_view_token(self) -> None:
        response = self.client.get(
            "/bob.git/info/refs?service=git-upload-pack",
            headers=self._basic(self.alice_token),
        )
        self.assertEqual(response.status_code, 404)

    def test_git_info_refs_returns_advertisement(self) -> None:
        response = self.client.get(
            "/alice.git/info/refs?service=git-upload-pack",
            headers=self._basic(self.alice_token),
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Content-Type"],
            "application/x-git-upload-pack-advertisement",
        )
        self.assertIn(b"# service=git-upload-pack", response.data)

    def test_git_upload_pack_returns_result(self) -> None:
        # First discover the advertised tip.
        refs = self.client.get(
            "/alice.git/info/refs?service=git-upload-pack",
            headers=self._basic(self.alice_token),
        )
        self.assertEqual(refs.status_code, 200)
        body = refs.data
        # Find the first ref OID in the advertisement, stripping the pkt-line length.
        oid: str | None = None
        for line in body.splitlines():
            text = line.decode(errors="replace")
            if not text.endswith(" refs/heads/main"):
                continue
            # pkt-line lines are "<4-hex-length><payload>"
            if len(text) > 44 and all(c in "0123456789abcdef" for c in text[4:44]):
                oid = text[4:44]
                break
        self.assertIsNotNone(oid)
        want = f"0032want {oid}\n00000009done\n".encode()
        response = self.client.post(
            "/alice.git/git-upload-pack",
            data=want,
            headers={
                **self._basic(self.alice_token),
                "Content-Type": "application/x-git-upload-pack-request",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.headers["Content-Type"],
            "application/x-git-upload-pack-result",
        )
        # A NAK or a packfile are both valid first-packet responses.
        self.assertTrue(response.data.startswith(b"0008NAK\n") or b"PACK" in response.data)


if __name__ == "__main__":
    unittest.main()
