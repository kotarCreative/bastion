"""Tests for the Flask-hosted permission demo server (spec 013 surface)."""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import unittest
import urllib.parse
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

        self.app = create_app(self.forge)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def _b64(self, raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def _login_session(self, token: str) -> None:
        """Legacy cookie sessions no longer exist; this helper is gone."""
        raise AssertionError("cookie sessions were removed by spec 015 B2")

    # -- browser application shell ----------------------------------------

    def test_landing_serves_application_shell(self) -> None:
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/assets/app.js", response.data)
        self.assertIn(b"/assets/app.css", response.data)
        self.assertNotIn(b"<script>", response.data)  # external script only

    def test_assets_are_served(self) -> None:
        css = self.client.get("/assets/app.css")
        js = self.client.get("/assets/app.js")
        self.assertEqual(css.status_code, 200)
        self.assertEqual(js.status_code, 200)
        self.assertIn("text/css", css.content_type)
        self.assertIn("javascript", js.content_type)

    def test_application_never_persists_credentials(self) -> None:
        js = self.client.get("/assets/app.js").data.decode()
        self.assertNotIn("document.cookie", js)
        self.assertNotIn("localStorage", js)
        self.assertNotIn("sessionStorage", js)
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("outerHTML", js)
        self.assertIn("page memory only", js)
        self.assertIn("Bearer ", js)
        # The token input is cleared right after use.
        self.assertIn('tokenInput.value = ""', js)
        shell = self.client.get("/").data.decode()
        self.assertIn('type="password"', shell)
        shell_without_quotes = shell
        self.assertNotIn("<style", shell_without_quotes)

    def test_unknown_routes_are_generic(self) -> None:
        for path in ("/login", "/logout", "/whoami", "/evaluate", "/browse",
                     "/blob", "/history", "/alice.git", "/nope"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.data, b"not found")

    def test_security_headers_on_every_response(self) -> None:
        expected = {
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
        for path in ("/", "/assets/app.css", "/assets/app.js", "/nope"):
            with self.subTest(path=path):
                response = self.client.get(path)
                for key, value in expected.items():
                    self.assertEqual(response.headers.get(key), value)

    def test_no_cookies_ever_set(self) -> None:
        for path, method, kwargs in (
            ("/", "get", {}),
            ("/assets/app.js", "get", {}),
            ("/nope", "get", {}),
            ("/api/v1/session", "get", {"headers": _bearer_header(self.alice_token)}),
        ):
            response = getattr(self.client, method)(path, **kwargs)
            self.assertIsNone(response.headers.get("Set-Cookie"))

    # -- API surface (Bearer only, memory-only credentials) -----------------

    def test_api_requires_bearer_without_cookie_fallback(self) -> None:
        response = self.client.get("/api/v1/session")
        self.assertEqual(response.status_code, 401)
        response = self.client.get(
            "/api/v1/session",
            headers={"Authorization": "Bearer junk"},
        )
        self.assertEqual(response.status_code, 401)

    def test_session_envelope_shape(self) -> None:
        response = self.client.get("/api/v1/session", headers=_bearer_header(self.alice_token))
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["view_id"], "alice")
        self.assertEqual(body["principal"]["id"], "alice")
        self.assertIn("capabilities", body["descriptor"])
        self.assertIn("restrictions", body["descriptor"])
        self.assertNotIn("canonical_frontier", body)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_tree_scopes_to_visible_paths(self) -> None:
        response = self.client.get(
            "/api/v1/tree", headers=_bearer_header(self.alice_token)
        )
        self.assertEqual(response.status_code, 200)
        paths = {entry["path_display"] for entry in response.get_json()["entries"]}
        self.assertIn("visible", paths)
        self.assertNotIn("hidden", paths)

    def test_hidden_tree_prefix_is_generic_404(self) -> None:
        hidden = self._b64(b"hidden")
        for token in (self.alice_token, self.bob_token):
            response = self.client.get(
                "/api/v1/tree",
                query_string={"path_b64": hidden},
                headers=_bearer_header(token),
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.get_json(), {"error": "not-found"})

    def test_decisions_endpoint(self) -> None:
        response = self.client.post(
            "/api/v1/decisions",
            headers={**_bearer_header(self.alice_token), "Content-Type": "application/json"},
            json={"capability": "read", "path_b64": self._b64(b"visible/file.txt")},
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["allowed"])
        response = self.client.post(
            "/api/v1/decisions",
            headers={**_bearer_header(self.alice_token), "Content-Type": "application/json"},
            json={"capability": "read", "path_b64": self._b64(b"hidden/secret.txt")},
        )
        self.assertFalse(response.get_json()["allowed"])

    def test_history_envelope(self) -> None:
        response = self.client.get("/api/v1/history", headers=_bearer_header(self.alice_token))
        self.assertEqual(response.status_code, 200)
        commits = response.get_json()["commits"]
        self.assertTrue(commits)
        self.assertIn("oid", commits[0])
        self.assertIn("message_display", commits[0])

    # -- Git routes ----------------------------------------------------------

    def test_git_info_refs_requires_auth(self) -> None:
        response = self.client.get(
            "/alice.git/info/refs", query_string={"service": "git-upload-pack"}
        )
        self.assertEqual(response.status_code, 401)

    def test_git_info_refs_rejects_wrong_view_token(self) -> None:
        response = self.client.get(
            "/alice.git/info/refs",
            query_string={"service": "git-upload-pack"},
            headers={"Authorization": _basic_header("bob", self.bob_token)},
        )
        self.assertEqual(response.status_code, 404)

    def test_git_info_refs_returns_advertisement(self) -> None:
        response = self.client.get(
            "/alice.git/info/refs",
            query_string={"service": "git-upload-pack"},
            headers={"Authorization": _basic_header("alice", self.alice_token)},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"refs/heads/main", response.data)
        self.assertEqual(response.headers.get("Cache-Control"), "no-store")

    def test_git_upload_pack_returns_result(self) -> None:
        from http_server import pkt_line

        tip = forge.output(forge.view_repo(self.forge, "alice"), "rev-parse", "HEAD")
        payload = (
            pkt_line(f"want {tip} agent=bastion-test\n".encode())
            + b"0000"
            + b"0009done\n"
        )
        response = self.client.post(
            "/alice.git/git-upload-pack",
            data=payload,
            headers={
                "Authorization": _basic_header("alice", self.alice_token),
                "Content-Type": "application/x-git-upload-pack-request",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"PACK", response.data)


def _bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _basic_header(view: str, token: str) -> str:
    encoded = base64.b64encode(f"{view}:{token}".encode()).decode()
    return f"Basic {encoded}"


if __name__ == "__main__":
    unittest.main()
