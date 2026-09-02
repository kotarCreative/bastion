"""Tests for the Flask-hosted permission demo server."""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import forge
import demo_server
from demo_server import create_app
from http_server import add_token
from state import load_state, recover
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
        (self.source / "visible" / "blob.bin").write_bytes(b"\x00\x01binary")
        weird_name = b"weird-\xff.txt".decode("utf-8", "surrogateescape")
        (self.source / "visible" / weird_name).write_text("weird bytes\n")
        (self.source / "visible" / "trailing.txt ").write_text("padded\n")
        (self.source / "hidden" / "secret.txt").write_text("secret 1\n")
        env = dict(os.environ, GIT_AUTHOR_DATE="1700000000 +0000",
                   GIT_COMMITTER_DATE="1700000000 +0000")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "initial mixed state", env=env)

        (self.source / "visible" / "file.txt").write_text("visible 2\n")
        env = dict(os.environ, GIT_AUTHOR_DATE="1700000100 +0000",
                   GIT_COMMITTER_DATE="1700000100 +0000")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "second mixed state", env=env)

        self.forge = self.root / "forge"
        forge.init_forge(self.forge, self.source, "main")
        identity_harness.add_view(self.forge, "alice", ["visible"], ["visible"], "main")
        identity_harness.add_view(self.forge, "bob", ["visible"], ["visible"], "main")
        identity_harness.add_view(self.forge, "nadia", ["visible/file.txt"], [], "main")

        self.alice_token = add_token(self.forge, "alice")
        self.bob_token = add_token(self.forge, "bob")
        self.nadia_token = add_token(self.forge, "nadia")
        recover(self.forge, load_state(self.forge))

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

    def test_browse_json(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/browse?format=json")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["view"], "alice")
        self.assertEqual(data["path"], "")
        paths = [entry["path"] for entry in data["entries"]]
        self.assertIn("visible", paths)
        self.assertNotIn("hidden", paths)

    def test_blob_shows_visible_file(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/blob?path=visible/file.txt")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"visible 2", response.data)
        self.assertNotIn(b"secret", response.data)

    def test_blob_denies_hidden_path(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/blob?path=hidden/secret.txt")
        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"secret 1", response.data)

    def test_blob_missing_path_is_404(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/blob?path=visible/absent.txt")
        self.assertEqual(response.status_code, 404)

    def test_blob_invalid_path_is_404(self) -> None:
        self._login(self.alice_token)
        for path in ("../escape", "visible/../../etc", ""):
            response = self.client.get(f"/blob?path={path}")
            self.assertEqual(response.status_code, 404)

    def test_blob_directory_redirects_to_browse(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/blob?path=visible")
        self.assertEqual(response.status_code, 302)
        followed = self.client.get(response.headers["Location"])
        self.assertEqual(followed.status_code, 200)
        self.assertIn(b"file.txt", followed.data)

    def test_blob_raw_text(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/blob?path=visible/file.txt&raw=1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, b"visible 2\n")
        self.assertEqual(
            response.headers["Content-Type"], "text/plain; charset=utf-8"
        )

    def test_blob_raw_binary(self) -> None:
        self._login(self.alice_token)
        raw = self.client.get("/blob?path=visible/blob.bin&raw=1")
        self.assertEqual(raw.status_code, 200)
        self.assertEqual(raw.data, b"\x00\x01binary")
        self.assertEqual(
            raw.headers["Content-Type"], "application/octet-stream"
        )

        page = self.client.get("/blob?path=visible/blob.bin")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Binary file", page.data)

    def test_blob_json(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/blob?path=visible/file.txt&format=json")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["path"], "visible/file.txt")
        self.assertEqual(data["kind"], "blob")
        self.assertEqual(data["content"], "visible 2\n")
        self.assertFalse(data["binary"])

    def test_blob_missing_intermediate_directory_is_404(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/blob?path=visible/absent/deep.txt")
        self.assertEqual(response.status_code, 404)

    def test_blob_json_honors_display_limit(self) -> None:
        self._login(self.alice_token)
        original_limit = demo_server.BLOB_DISPLAY_LIMIT
        try:
            demo_server.BLOB_DISPLAY_LIMIT = 1
            response = self.client.get("/blob?path=visible/file.txt&format=json")
            self.assertEqual(response.status_code, 200)
            data = response.get_json()
            self.assertTrue(data["too_large"])
            self.assertIsNone(data["content"])
        finally:
            demo_server.BLOB_DISPLAY_LIMIT = original_limit

    def test_blob_raw_streams_large_content(self) -> None:
        self._login(self.alice_token)
        original_limit = demo_server.BLOB_DISPLAY_LIMIT
        try:
            demo_server.BLOB_DISPLAY_LIMIT = 1
            response = self.client.get("/blob?path=visible/file.txt&raw=1")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"visible 2\n")
            self.assertEqual(
                response.headers["Content-Type"], "text/plain; charset=utf-8"
            )
        finally:
            demo_server.BLOB_DISPLAY_LIMIT = original_limit

    def test_browse_denies_hidden_directory(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/browse?path=hidden")
        self.assertEqual(response.status_code, 404)

    def test_browse_allows_visible_directory(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/browse?path=visible")
        self.assertEqual(response.status_code, 200)
        text = response.data.decode()
        self.assertIn("file.txt", text)
        self.assertNotIn("secret.txt", text)

    def test_history_scoped_to_visible_path(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/history?path=visible/file.txt")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"visible/file.txt", response.data)
        self.assertIn(b"projected canonical change", response.data)

    def test_history_denies_hidden_path(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/history?path=hidden/secret.txt")
        self.assertEqual(response.status_code, 404)

    def test_history_json(self) -> None:
        self._login(self.alice_token)
        response = self.client.get("/history?format=json")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(data["view"], "alice")
        self.assertEqual(data["path"], "")
        self.assertTrue(data["commits"])
        self.assertIn("oid", data["commits"][0])
        self.assertIn("message", data["commits"][0])

    def test_narrow_grant_directories_remain_traversable(self) -> None:
        self._login(self.nadia_token)
        root = self.client.get("/browse?format=json")
        self.assertEqual(root.status_code, 200)
        entries = {entry["path"]: entry for entry in root.get_json()["entries"]}
        self.assertIn("visible", entries)
        self.assertNotIn("hidden", entries)

        scoped = self.client.get("/browse?path=visible")
        self.assertEqual(scoped.status_code, 200)
        text = scoped.data.decode()
        self.assertIn("file.txt", text)
        self.assertNotIn("blob.bin", text)

        directory = self.client.get("/blob?path=visible")
        self.assertEqual(directory.status_code, 302)
        followed = self.client.get(directory.headers["Location"])
        self.assertEqual(followed.status_code, 200)
        self.assertIn(b"file.txt", followed.data)

        denied = self.client.get("/blob?path=visible/blob.bin")
        self.assertEqual(denied.status_code, 404)

        history = self.client.get("/history?path=visible")
        self.assertEqual(history.status_code, 200)
        self.assertIn(b"projected canonical change", history.data)

    def test_non_utf8_path_round_trips(self) -> None:
        self._login(self.alice_token)
        encoded = base64.urlsafe_b64encode(b"visible/weird-\xff.txt").decode("ascii")

        page = self.client.get(f"/blob?path_b64={encoded}")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"weird bytes", page.data)

        raw = self.client.get(f"/blob?path_b64={encoded}&raw=1")
        self.assertEqual(raw.status_code, 200)
        self.assertEqual(raw.data, b"weird bytes\n")

        fallback = self.client.get("/blob?path=visible/weird-%FF.txt&raw=1")
        self.assertEqual(fallback.status_code, 200)
        self.assertEqual(fallback.data, b"weird bytes\n")

        listing = self.client.get("/browse?path=visible&format=json")
        data = listing.get_json()
        matches = [entry for entry in data["entries"] if entry["path_b64"] == encoded]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["path"], "visible/weird-\ufffd.txt")

    def test_history_preserves_path_whitespace(self) -> None:
        self._login(self.alice_token)
        blob = self.client.get("/blob?path=visible/trailing.txt%20&raw=1")
        self.assertEqual(blob.status_code, 200)
        self.assertEqual(blob.data, b"padded\n")

        history = self.client.get("/history?path=visible/trailing.txt%20")
        self.assertEqual(history.status_code, 200)
        self.assertIn(b"projected canonical change", history.data)
        self.assertNotIn(b"No history is visible", history.data)

    def test_nul_byte_paths_are_404(self) -> None:
        self._login(self.alice_token)
        for route in ("browse", "blob", "history"):
            response = self.client.get(f"/{route}?path=visible/%00x")
            self.assertEqual(response.status_code, 404, route)
        encoded = base64.urlsafe_b64encode(b"visible/\x00x").decode("ascii")
        response = self.client.get(f"/blob?path_b64={encoded}")
        self.assertEqual(response.status_code, 404)

    def test_blob_raw_stream_limit_returns_503(self) -> None:
        self._login(self.alice_token)
        original = demo_server._blob_stream_slots
        try:
            demo_server._blob_stream_slots = threading.BoundedSemaphore(0)
            response = self.client.get("/blob?path=visible/file.txt&raw=1")
            self.assertEqual(response.status_code, 503)
        finally:
            demo_server._blob_stream_slots = original

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
