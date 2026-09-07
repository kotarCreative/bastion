"""Spec 015 A3: route-by-scope matrix across every authenticated surface.

Covers fetch-only, push-only, ui-only, combined, malformed, expired,
revoked, and cross-view credentials against the demo API, the demo
browser routes, Git advertisements, and Git RPCs on both hosted servers.
Unauthorized requests perform no repository work; unknown-view and
cross-view credentials stay indistinguishable.
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import demo_server
import forge
from demo_server import create_app
from http_server import add_token, make_server
from state import load_state, recover
from tests import identity_harness
from tests.storage_helpers import repository_snapshot
from tests.tree_harness import command, git


def _basic_header(token: str, view: str) -> str:
    encoded = base64.b64encode(f"{view}:{token}".encode()).decode()
    return f"Basic {encoded}"


class ScopeMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        source = root / "source"
        command("git", "init", "-q", "-b", "main", str(source))
        (source / "visible").mkdir()
        (source / "hidden").mkdir()
        (source / "visible" / "file.txt").write_text("visible 1\n")
        (source / "hidden" / "secret.txt").write_text("secret 1\n")
        environment = dict(
            os.environ,
            GIT_AUTHOR_DATE="1700000000 +0000",
            GIT_COMMITTER_DATE="1700000000 +0000",
        )
        git(source, "add", "-A", env=environment)
        git(source, "commit", "-q", "-m", "initial", env=environment)
        self.forge = root / "forge"
        forge.init_forge(self.forge, source, "main")
        identity_harness.add_view(self.forge, "alice", ["visible"], ["visible"], "main")
        identity_harness.add_view(self.forge, "bob", ["visible"], ["visible"], "main")
        recover(self.forge, load_state(self.forge))

        self.tokens = {
            "fetch-only": add_token(self.forge, "alice", scopes=["fetch"]),
            "push-only": add_token(self.forge, "alice", scopes=["push"]),
            "ui-only": add_token(self.forge, "alice", scopes=["ui"]),
            "fetch+push": add_token(self.forge, "alice", scopes=["fetch", "push"]),
            "fetch+ui": add_token(self.forge, "alice", scopes=["fetch", "ui"]),
            "push+ui": add_token(self.forge, "alice", scopes=["push", "ui"]),
            "all": add_token(self.forge, "alice", scopes=["fetch", "push", "ui"]),
            "cross-view": add_token(self.forge, "bob", scopes=["fetch", "push", "ui"]),
            "expired": add_token(self.forge, "alice", scopes=["ui"], expires_in="1m"),
            "revoked": add_token(self.forge, "alice", scopes=["fetch", "push", "ui"]),
        }
        from http_server import revoke_token

        revoke_token(self.forge, self.tokens["revoked"].split(".")[1])
        self.malformed = "bst1.AAAAAAAAAAAAAAAAAAAAAA.AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"

        os.environ["BASTION_DEMO_SECRET"] = "test-secret-do-not-use"
        self.addCleanup(os.environ.pop, "BASTION_DEMO_SECRET", None)
        self.app = create_app(self.forge)
        self.client = self.app.test_client()

    # -- helpers ---------------------------------------------------------

    def snapshot(self):
        return repository_snapshot(self.forge)

    def bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def api_status(self, token: str, path: str = "/api/v1/session") -> int:
        headers = {} if token is None else self.bearer(token)
        return self.client.get(path, headers=headers).status_code

    def git_status(
        self, token: str, view: str, service: str, *, post: bool = False
    ) -> int:
        if post:
            response = self.client.post(
                f"/{view}.git/{service}",
                data=b"0000",
                headers={"Authorization": _basic_header(token, view)},
            )
        else:
            response = self.client.get(
                f"/{view}.git/info/refs",
                query_string={"service": service},
                headers={"Authorization": _basic_header(token, view)},
            )
        return response.status_code

    def assert_snapshot_unchanged(self, before) -> None:
        self.assertEqual(before, self.snapshot())

    # -- API surface -----------------------------------------------------

    def test_api_session_scope_matrix(self) -> None:
        matrix = {
            "fetch-only": 403,
            "push-only": 403,
            "fetch+push": 403,
            "ui-only": 200,
            "fetch+ui": 200,
            "push+ui": 200,
            "all": 200,
            "malformed": 401,
            "expired": 401,
            "revoked": 401,
            None: 401,
        }
        for credential, expected in matrix.items():
            with self.subTest(credential=credential):
                if credential == "expired":
                    with patch("time.time", return_value=2_000_000_000):
                        status = self.api_status(self.tokens[credential])
                else:
                    token = self.tokens.get(credential, self.malformed)
                    status = self.api_status(token)
                self.assertEqual(status, expected)

    def test_api_routes_all_require_ui(self) -> None:
        for path, method in (
            ("/api/v1/tree", "get"),
            ("/api/v1/history", "get"),
            ("/api/v1/decisions", "post"),
        ):
            with self.subTest(path=path):
                push_only = self.client.open(
                    path,
                    method=method,
                    headers=self.bearer(self.tokens["push-only"]),
                    json={"capability": "read", "path_b64": ""} if method == "post" else None,
                )
                self.assertEqual(push_only.status_code, 403)
                granted = self.client.open(
                    path,
                    method=method,
                    headers=self.bearer(self.tokens["all"]),
                    json={"capability": "read", "path_b64": ""} if method == "post" else None,
                )
                self.assertEqual(granted.status_code, 200)

    def test_api_bearer_only_without_cookie_fallback(self) -> None:
        self.assertEqual(self.client.get("/api/v1/session").status_code, 401)

    # -- Git surface -----------------------------------------------------

    def test_git_advertisement_scope_matrix(self) -> None:
        fetch_matrix = {
            "fetch-only": 200,
            "push-only": 403,
            "ui-only": 403,
            "fetch+push": 200,
            "all": 200,
            "malformed": 401,
            "expired": 401,
            "revoked": 401,
        }
        for credential, expected in fetch_matrix.items():
            with self.subTest(credential=credential, service="upload"):
                if credential == "expired":
                    with patch("time.time", return_value=2_000_000_000):
                        status = self.git_status(
                            self.tokens[credential], "alice", "git-upload-pack"
                        )
                else:
                    status = self.git_status(
                        self.tokens.get(credential, self.malformed),
                        "alice",
                        "git-upload-pack",
                    )
                self.assertEqual(status, expected)
        push_matrix = dict(fetch_matrix, **{"fetch-only": 403, "push-only": 200})
        for credential, expected in push_matrix.items():
            with self.subTest(credential=credential, service="receive"):
                if credential == "expired":
                    with patch("time.time", return_value=2_000_000_000):
                        status = self.git_status(
                            self.tokens[credential], "alice", "git-receive-pack"
                        )
                else:
                    status = self.git_status(
                        self.tokens.get(credential, self.malformed),
                        "alice",
                        "git-receive-pack",
                    )
                self.assertEqual(status, expected)

    def test_git_rpc_scope_matrix(self) -> None:
        # The scope decision precedes body reading and Git execution.
        before = self.snapshot()
        self.assertEqual(
            self.git_status(self.tokens["ui-only"], "alice", "git-upload-pack", post=True),
            403,
        )
        self.assertEqual(
            self.git_status(self.tokens["ui-only"], "alice", "git-receive-pack", post=True),
            403,
        )
        self.assertEqual(
            self.git_status(self.tokens["push-only"], "alice", "git-upload-pack", post=True),
            403,
        )
        self.assert_snapshot_unchanged(before)
        # push-only may reach receive; fetch-only may reach upload.
        self.assertNotIn(
            self.git_status(self.tokens["push-only"], "alice", "git-receive-pack", post=True),
            (401, 403, 405),
        )
        self.assertNotIn(
            self.git_status(self.tokens["fetch-only"], "alice", "git-upload-pack", post=True),
            (401, 403, 405),
        )

    def test_git_http_server_scope_matrix(self) -> None:
        server = make_server(self.forge, "127.0.0.1", 0)
        port = server.server_address[1]
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)

        def raw_request(path: str, headers: dict[str, str], data: bytes | None = None) -> int:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", data=data, headers=headers
            )
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status
            except urllib.error.HTTPError as error:
                return error.code

        base = {"Authorization": _basic_header(self.tokens["ui-only"], "alice")}
        self.assertEqual(
            raw_request(
                "/alice.git/info/refs?service=git-upload-pack", base
            ),
            403,
        )
        self.assertEqual(
            raw_request(
                "/alice.git/git-receive-pack",
                base,
                data=b"0000",
            ),
            403,
        )
        push_only = {"Authorization": _basic_header(self.tokens["push-only"], "alice")}
        self.assertEqual(
            raw_request(
                "/alice.git/info/refs?service=git-upload-pack", push_only
            ),
            403,
        )
        self.assertNotIn(
            raw_request(
                "/alice.git/info/refs?service=git-receive-pack", push_only
            ),
            (401, 403, 405),
        )
        fetch_only = {"Authorization": _basic_header(self.tokens["fetch-only"], "alice")}
        self.assertEqual(
            raw_request(
                "/alice.git/info/refs?service=git-receive-pack", fetch_only
            ),
            403,
        )
        self.assertNotIn(
            raw_request(
                "/alice.git/info/refs?service=git-upload-pack", fetch_only
            ),
            (401, 403, 405),
        )

    def test_cross_view_and_unknown_views_indistinguishable(self) -> None:
        cross = self.client.get(
            "/alice.git/info/refs",
            query_string={"service": "git-upload-pack"},
            headers={"Authorization": _basic_header(self.tokens["cross-view"], "alice")},
        )
        unknown = self.client.get(
            "/unknown.git/info/refs",
            query_string={"service": "git-upload-pack"},
            headers={"Authorization": _basic_header(self.tokens["cross-view"], "unknown")},
        )
        self.assertEqual(cross.status_code, 404)
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(cross.data, unknown.data)
        server = make_server(self.forge, "127.0.0.1", 0)
        port = server.server_address[1]
        import threading

        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)

        def raw_request(path: str, headers: dict[str, str]) -> tuple[int, bytes]:
            request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
            try:
                with urllib.request.urlopen(request) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()

        first = raw_request(
            "/alice.git/info/refs?service=git-upload-pack",
            {"Authorization": _basic_header(self.tokens["cross-view"], "alice")},
        )
        second = raw_request(
            "/unknown.git/info/refs?service=git-upload-pack",
            {"Authorization": _basic_header(self.tokens["cross-view"], "unknown")},
        )
        self.assertEqual(first, second)

    def test_revocation_applies_to_next_request(self) -> None:
        credential = self.tokens["all"]
        self.assertEqual(self.api_status(credential), 200)
        self.assertEqual(
            self.git_status(credential, "alice", "git-upload-pack"), 200
        )
        from http_server import revoke_token

        revoke_token(self.forge, credential.split(".")[1])
        self.assertEqual(self.api_status(credential), 401)
        self.assertEqual(self.git_status(credential, "alice", "git-upload-pack"), 401)
        self.assertEqual(
            self.git_status(credential, "alice", "git-receive-pack"), 401
        )

    # -- browser surface (spec-013 final form) -----------------------------

    def test_legacy_browser_routes_are_removed(self) -> None:
        for path in ("/login", "/logout", "/whoami", "/evaluate", "/browse",
                     "/blob", "/history"):
            with self.subTest(path=path):
                response = self.client.post(path, data={"token": self.tokens["all"]})
                self.assertEqual(response.status_code, 404)
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)

    def test_memory_only_credential_flow(self) -> None:
        # The page-memory flow validates credentials through the API; the
        # server sets no cookies and a push-only credential cannot open the
        # interface (403) while unauthenticated requests stay 401.
        self.assertEqual(self.client.get("/api/v1/session").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/api/v1/session", headers=self.bearer(self.tokens["push-only"])
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/api/v1/session", headers=self.bearer(self.tokens["all"])
            ).status_code,
            200,
        )
        for response in (
            self.client.get("/"),
            self.client.get("/assets/app.js"),
            self.client.get("/assets/app.css"),
        ):
            self.assertIsNone(response.headers.get("Set-Cookie"))
        js = self.client.get("/assets/app.js").data.decode()
        self.assertNotIn("document.cookie", js)
        self.assertNotIn("Storage", js)
        self.assertNotIn("innerHTML", js)

    def test_unauthorized_requests_do_no_repository_work(self) -> None:
        before = self.snapshot()
        self.assertEqual(
            self.git_status(self.tokens["ui-only"], "alice", "git-receive-pack"), 403
        )
        self.assertEqual(
            self.git_status(self.tokens["push-only"], "alice", "git-upload-pack"), 403
        )
        self.assertEqual(self.api_status(self.tokens["push-only"]), 403)
        self.assertEqual(self.api_status(self.malformed), 401)
        self.assert_snapshot_unchanged(before)
        forge.check_invariants(self.forge)


if __name__ == "__main__":
    unittest.main()
