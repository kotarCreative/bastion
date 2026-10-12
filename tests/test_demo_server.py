"""Tests for the Flask-hosted permission demo server (spec 013 surface)."""

from __future__ import annotations

import base64
import os
import subprocess
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch

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
        self.assertIn(b'id="clerk-sign-in"', response.data)
        self.assertIn(b'id="tab-permissions"', response.data)
        self.assertIn(b'id="tab-profile"', response.data)
        self.assertIn(b'id="tree-list"', response.data)
        self.assertIn(b'id="clone-dialog"', response.data)
        self.assertNotIn(b"<script>", response.data)  # external script only

    def test_auth_config_is_disabled_without_clerk_environment(self) -> None:
        response = self.client.get("/api/v1/auth/config")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"enabled": False})

    def test_assets_are_served(self) -> None:
        css = self.client.get("/assets/app.css")
        js = self.client.get("/assets/app.js")
        self.assertEqual(css.status_code, 200)
        self.assertEqual(js.status_code, 200)
        self.assertIn("text/css", css.content_type)
        self.assertIn("javascript", js.content_type)

    def test_assets_revalidate_with_etag(self) -> None:
        """A stale shell must never be served from an unconditional cache."""
        page = self.client.get("/")
        asset = self.client.get("/assets/app.js")
        for response in (page, asset):
            with self.subTest(path=response.request.path):
                self.assertEqual(response.headers.get("Cache-Control"), "no-cache")
                self.assertTrue(response.headers.get("ETag"))
        # A matching validator yields an empty 304, so the browser reuses its
        # copy without a second download.
        etag = asset.headers["ETag"]
        cached = self.client.get("/assets/app.js", headers={"If-None-Match": etag})
        self.assertEqual(cached.status_code, 304)
        self.assertEqual(cached.data, b"")
        # A changed asset changes the validator, forcing a fresh body.
        stale = self.client.get("/assets/app.js", headers={"If-None-Match": '"stale"'})
        self.assertEqual(stale.status_code, 200)
        self.assertTrue(stale.data)

    def test_application_never_persists_credentials(self) -> None:
        js = self.client.get("/assets/app.js").data.decode()
        self.assertNotIn("document.cookie", js)
        self.assertNotIn("localStorage", js)
        self.assertNotIn("sessionStorage", js)
        self.assertNotIn("innerHTML", js)
        self.assertNotIn("outerHTML", js)
        self.assertIn("page memory only", js)
        self.assertIn("Bearer ", js)
        # The Git/API token input is cleared right after use.
        self.assertIn('tokenInput.value = ""', js)
        shell = self.client.get("/").data.decode()
        self.assertNotIn('name="password"', shell)
        self.assertNotIn("<style", shell)

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

    def test_clerk_session_claims_invited_principal_and_issues_ui_token(self) -> None:
        from state import locked, save_state

        encoded_domain = base64.urlsafe_b64encode(
            b"clerk.example.test$"
        ).decode("ascii").rstrip("=")
        environment = {
            "CLERK_PUBLISHABLE_KEY": f"pk_test_{encoded_domain}",
            "CLERK_SECRET_KEY": "sk_test_secret",
            "CLERK_AUTHORIZED_PARTIES": "https://bastion.example.test",
        }
        with locked(self.forge) as state:
            state["principals"]["alice"]["emails"] = ["alice@example.test"]
            save_state(self.forge, state)
        with patch.dict(os.environ, environment, clear=False):
            app = create_app(self.forge)

        class FakeAuthenticator:
            def authenticate(self, _headers):
                return "user_clerkAlice", ["alice@example.test"]

        app.config["CLERK_AUTHENTICATOR"] = FakeAuthenticator()
        client = app.test_client()
        config = client.get("/api/v1/auth/config")
        self.assertEqual(config.status_code, 200)
        self.assertEqual(config.get_json()["frontend_api_origin"], "https://clerk.example.test")
        self.assertIn(
            "https://clerk.example.test",
            config.headers["Content-Security-Policy"],
        )
        accepted = client.post(
            "/api/v1/auth/clerk",
            json={},
            headers={"Authorization": "Bearer clerk-session"},
        )
        self.assertEqual(accepted.status_code, 200)
        body = accepted.get_json()
        self.assertEqual(body["session"]["principal"]["id"], "alice")
        self.assertEqual(
            load_state(self.forge)["principals"]["alice"]["clerk_user_id"],
            "user_clerkAlice",
        )
        self.assertEqual(
            client.get(
                "/api/v1/session", headers=_bearer_header(body["access_token"])
            ).status_code,
            200,
        )
        bearer = _bearer_header(body["access_token"])
        self.assertEqual(client.post("/api/v1/logout", headers=bearer).status_code, 200)
        self.assertEqual(client.get("/api/v1/session", headers=bearer).status_code, 401)

    def test_permissions_require_repository_owner(self) -> None:
        self.assertEqual(self.client.get("/api/v1/permissions").status_code, 401)
        self.assertEqual(self.client.post("/api/v1/permissions").status_code, 401)
        fetch_only = add_token(self.forge, "alice", ["fetch"])
        self.assertEqual(
            self.client.get(
                "/api/v1/permissions", headers=_bearer_header(fetch_only)
            ).status_code,
            403,
        )
        response = self.client.get(
            "/api/v1/permissions", headers=_bearer_header(self.alice_token)
        )
        self.assertEqual(response.status_code, 403)

    def test_owner_can_make_file_private_and_restore_visibility(self) -> None:
        from identity import CAPABILITIES
        from service import add_role, bind_role, transition_principal

        allow = {capability: [] for capability in CAPABILITIES}
        deny = {capability: [] for capability in CAPABILITIES}
        allow["admin"] = [""]
        add_role(self.forge, "alice-owner", allow, deny)
        bind_role(self.forge, "alice-owner", "principal", "alice")
        transition_principal(self.forge, "alice")

        permissions = self.client.get(
            "/api/v1/permissions", headers=_bearer_header(self.alice_token)
        )
        self.assertEqual(permissions.status_code, 200)
        self.assertIn("bob", {item["view_id"] for item in permissions.get_json()["targets"]})

        request_body = {
            "view_id": "bob",
            "path_b64": self._b64(b"visible/file.txt"),
            "private": True,
        }
        hidden = self.client.post(
            "/api/v1/permissions",
            headers={**_bearer_header(self.alice_token), "Content-Type": "application/json"},
            json=request_body,
        )
        self.assertEqual(hidden.status_code, 200, hidden.get_data(as_text=True))
        self.assertEqual(
            [item["prefix_display"] for item in hidden.get_json()["private_paths"]],
            ["visible/file.txt"],
        )
        bob_tree = self.client.get(
            "/api/v1/tree",
            query_string={"path_b64": self._b64(b"visible")},
            headers=_bearer_header(self.bob_token),
        )
        self.assertEqual(bob_tree.status_code, 200)
        self.assertNotIn(
            "visible/file.txt",
            {item["path_display"] for item in bob_tree.get_json()["entries"]},
        )

        request_body["private"] = False
        restored = self.client.post(
            "/api/v1/permissions",
            headers={**_bearer_header(self.alice_token), "Content-Type": "application/json"},
            json=request_body,
        )
        self.assertEqual(restored.status_code, 200)
        bob_tree = self.client.get(
            "/api/v1/tree",
            query_string={"path_b64": self._b64(b"visible")},
            headers=_bearer_header(self.bob_token),
        )
        self.assertIn(
            "visible/file.txt",
            {item["path_display"] for item in bob_tree.get_json()["entries"]},
        )

    def test_tree_scopes_to_visible_paths(self) -> None:
        response = self.client.get(
            "/api/v1/tree", headers=_bearer_header(self.alice_token)
        )
        self.assertEqual(response.status_code, 200)
        paths = {entry["path_display"] for entry in response.get_json()["entries"]}
        self.assertIn("visible", paths)
        self.assertNotIn("hidden", paths)

    def test_blob_reads_only_visible_text_files(self) -> None:
        visible = self.client.get(
            "/api/v1/blob",
            query_string={"path_b64": self._b64(b"visible/file.txt")},
            headers=_bearer_header(self.alice_token),
        )
        self.assertEqual(visible.status_code, 200)
        self.assertEqual(visible.get_json()["text"], "visible 2\n")
        self.assertFalse(visible.get_json()["binary"])
        self.assertEqual(
            set(visible.get_json()),
            {"projected_tip", "path_b64", "path_display", "size", "binary", "text"},
        )

        binary = self.client.get(
            "/api/v1/blob",
            query_string={"path_b64": self._b64(b"visible/blob.bin")},
            headers=_bearer_header(self.alice_token),
        )
        self.assertEqual(binary.status_code, 200)
        self.assertTrue(binary.get_json()["binary"])
        self.assertIsNone(binary.get_json()["text"])

        for path in (b"hidden/secret.txt", b"missing.txt", b"visible"):
            response = self.client.get(
                "/api/v1/blob",
                query_string={"path_b64": self._b64(path)},
                headers=_bearer_header(self.alice_token),
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.get_json(), {"error": "not-found"})

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

    # -- Git credentials ------------------------------------------------------

    def test_git_credential_lifecycle_and_authorization(self) -> None:
        import tokens as _tokens
        from identity import empty_capability_map
        from service import add_principal, add_role, add_view, bind_role

        owner = self._owner_token()      # read=[""], admin=[""], no write
        headers = self._bearer(owner)

        # A read-only principal for cross-principal checks.
        add_principal(self.forge, "reader", ["reader@example.test"])
        allow = empty_capability_map()
        allow["read"] = ["visible"]
        add_role(self.forge, "reader-role", allow, empty_capability_map())
        bind_role(self.forge, "reader-role", "principal", "reader")
        add_view(self.forge, "reader", "reader", "main")
        reader = add_token(self.forge, "reader", ["ui"], "1h")

        self.assertEqual(self.client.get("/api/v1/credentials").status_code, 401)
        listing = self.client.get("/api/v1/credentials", headers=headers)
        self.assertEqual(listing.status_code, 200)
        body = listing.get_json()
        self.assertEqual(body["principal_id"], "owner")
        self.assertEqual(body["views"], [{"view_id": "owner", "can_push": False}])
        # The browser ui session token is not offered as a Git credential.
        self.assertEqual(body["tokens"], [])

        # push requires a write capability the owner does not hold
        response = self.client.post("/api/v1/credentials", headers=headers, json={
            "view_id": "owner", "scopes": ["fetch", "push"], "expires_in": "30d"})
        self.assertEqual(response.status_code, 403)

        # fetch is allowed; the secret is returned exactly once
        response = self.client.post("/api/v1/credentials", headers=headers, json={
            "view_id": "owner", "scopes": ["fetch"], "expires_in": "30d"})
        self.assertEqual(response.status_code, 201)
        credential = response.get_json()["credential"]
        self.assertRegex(credential, _tokens.TOKEN_RE)
        token_id = credential.split(".")[1]
        # A fetch credential authenticates Git, not the browser API (no ui scope).
        basic = base64.b64encode(f"owner:{credential}".encode()).decode()
        self.assertEqual(self.client.get(
            "/owner.git/info/refs?service=git-upload-pack",
            headers={"Authorization": f"Basic {basic}"},
        ).status_code, 200)
        self.assertEqual(self.client.get(
            "/api/v1/session", headers={"Authorization": f"Bearer {credential}"}
        ).status_code, 403)
        listed = self.client.get("/api/v1/credentials", headers=headers).get_json()["tokens"]
        self.assertEqual([item["token_id"] for item in listed], [token_id])
        self.assertEqual(listed[0]["scopes"], ["fetch"])

        # a write-capable principal may mint a push credential
        add_principal(self.forge, "writer", ["writer@example.test"])
        allow = empty_capability_map()
        allow["read"] = ["visible"]
        allow["create"] = ["visible"]
        allow["modify"] = ["visible"]
        add_role(self.forge, "writer-role", allow, empty_capability_map())
        bind_role(self.forge, "writer-role", "principal", "writer")
        add_view(self.forge, "writer", "writer", "main")
        writer = add_token(self.forge, "writer", ["ui"], "1h")
        self.assertEqual(self.client.post("/api/v1/credentials", headers=self._bearer(writer), json={
            "view_id": "writer", "scopes": ["fetch", "push"], "expires_in": "7d",
        }).status_code, 201)
        self.assertTrue(self.client.get(
            "/api/v1/credentials", headers=self._bearer(writer)
        ).get_json()["views"][0]["can_push"])

        # a principal cannot mint for a view it does not own
        self.assertEqual(self.client.post("/api/v1/credentials", headers=self._bearer(writer), json={
            "view_id": "owner", "scopes": ["fetch"], "expires_in": "30d",
        }).status_code, 403)

        for change in ({"scopes": []}, {"scopes": ["admin"]}, {"scopes": ["ui"]},
                       {"expires_in": "forever"}, {"expires_in": "400d"}, {"view_id": "nope"}):
            payload = {"view_id": "owner", "scopes": ["fetch"], "expires_in": "30d"} | change
            with self.subTest(change=change):
                self.assertIn(self.client.post(
                    "/api/v1/credentials", headers=headers, json=payload
                ).status_code, {400, 403})

        # revoke: another principal's id and an unknown id are one 404
        self.assertEqual(self.client.delete(
            f"/api/v1/credentials/{token_id}", headers=self._bearer(reader)).status_code, 404)
        self.assertEqual(self.client.delete(
            "/api/v1/credentials/" + "A" * 22, headers=headers).status_code, 404)
        self.assertEqual(self.client.delete(
            f"/api/v1/credentials/{token_id}", headers=headers).status_code, 200)
        self.assertEqual(self.client.get(
            "/owner.git/info/refs?service=git-upload-pack",
            headers={"Authorization": f"Basic {basic}"},
        ).status_code, 401)
        forge.check_invariants(self.forge)

    # -- Git routes ----------------------------------------------------------

    def _owner_token(self) -> str:
        from identity import empty_capability_map
        from service import add_principal, add_role, bind_role, add_view

        add_principal(self.forge, "owner", ["owner@example.test"])
        allow = empty_capability_map()
        allow["read"] = [""]
        allow["admin"] = [""]
        add_role(self.forge, "owner-role", allow, empty_capability_map())
        bind_role(self.forge, "owner-role", "principal", "owner")
        add_view(self.forge, "owner", "owner", "main")
        return add_token(self.forge, "owner")

    def test_invitation_applies_scoped_permissions_before_first_login(self) -> None:
        from accounts import link_clerk_account

        owner = self._owner_token()
        for access, name in (("read-only", "reader"), ("contributor", "writer")):
            with self.subTest(access=access):
                response = self.client.post(
                    "/api/v1/invitations", headers=self._bearer(owner),
                    json={"principal_id": name, "email": name + "@example.test",
                          "access": access, "readable_paths": ["visible/file.txt"]},
                )
                self.assertEqual(response.status_code, 201, response.get_json())
                self.assertEqual(link_clerk_account(self.forge, "user_" + name, [name + "@example.test"]), name)
                token = add_token(self.forge, name)
                envelope = self.client.get("/api/v1/session", headers=self._bearer(token)).get_json()
                capabilities = envelope["descriptor"]["capabilities"]
                self.assertEqual(bool(capabilities["modify"]), access == "contributor")
                self.assertEqual(capabilities["admin"], [])
                root = self.client.get("/api/v1/tree", headers=self._bearer(token)).get_json()
                self.assertEqual([item["path_display"] for item in root["entries"]], ["visible"])
                children = self.client.get("/api/v1/tree", query_string={"path_b64": self._b64(b"visible")}, headers=self._bearer(token)).get_json()
                self.assertEqual([item["path_display"] for item in children["entries"]], ["visible/file.txt"])
                for path in (b"hidden/secret.txt", b"visible/blob.bin"):
                    hidden = self.client.get("/api/v1/blob", query_string={"path_b64": self._b64(path)}, headers=self._bearer(token))
                    self.assertEqual(hidden.status_code, 404)
                self.assertEqual(self.client.get("/api/v1/permissions", headers=self._bearer(token)).status_code, 403)
        forge.check_invariants(self.forge)

    def test_invitation_rejects_nonowners_invalid_paths_and_duplicates(self) -> None:
        payload = {"principal_id": "invitee", "email": "invitee@example.test",
                   "access": "read-only", "readable_paths": ["visible"]}
        self.assertEqual(self.client.post("/api/v1/invitations", json=payload).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/invitations", json=payload, headers=self._bearer(self.alice_token)).status_code, 403)
        owner = self._owner_token()
        for change in ({"readable_paths": ["missing"]}, {"access": "admin"}, {"readable_paths": ["../hidden"]}, {"readable_paths": ["visible\x00"]}, {"readable_paths": []}):
            response = self.client.post("/api/v1/invitations", json=payload | change, headers=self._bearer(owner))
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("invitee", load_state(self.forge)["principals"])
        self.assertEqual(self.client.post("/api/v1/invitations", json=payload, headers=self._bearer(owner)).status_code, 201)
        for change in ({}, {"principal_id": "other"}, {"email": "another@example.test"}):
            self.assertEqual(self.client.post("/api/v1/invitations", json=payload | change, headers=self._bearer(owner)).status_code, 409)

    def test_owner_can_hide_and_restore_invited_contributor_paths(self) -> None:
        owner = self._owner_token()
        self.client.post("/api/v1/invitations", headers=self._bearer(owner), json={
            "principal_id": "invitee", "email": "invitee@example.test",
            "access": "contributor", "readable_paths": ["/"],
        })
        token = add_token(self.forge, "invitee")
        before = self.client.get("/api/v1/session", headers=self._bearer(token)).get_json()
        for private in (True, False):
            response = self.client.post("/api/v1/permissions", headers=self._bearer(owner), json={
                "view_id": "invitee", "path_b64": self._b64(b"hidden"), "private": private,
            })
            self.assertEqual(response.status_code, 200)
            tree = self.client.get("/api/v1/tree", headers=self._bearer(token)).get_json()
            self.assertEqual("hidden" in [item["path_display"] for item in tree["entries"]], not private)
            for capability in ("read", "modify"):
                decision = self.client.post("/api/v1/decisions", headers=self._bearer(token), json={
                    "capability": capability, "path_b64": self._b64(b"hidden/secret.txt"),
                }).get_json()
                self.assertEqual(decision["allowed"], not private)
        after = self.client.get("/api/v1/session", headers=self._bearer(token)).get_json()
        self.assertGreater(after["descriptor"]["projection_epoch"], before["descriptor"]["projection_epoch"])
        forge.check_invariants(self.forge)

    def test_rejected_invitation_fence_rolls_back_catalog(self) -> None:
        from errors import ForgeError
        from repository import Repository
        from service import invite_collaborator

        self._owner_token()
        with patch.object(Repository, "publish", side_effect=ForgeError("ownership changed", "conflict")):
            with self.assertRaises(ForgeError):
                invite_collaborator(self.forge, "owner", "invitee", "invitee@example.test", ["visible"], "read-only")
        self.assertNotIn("invitee", load_state(self.forge)["principals"])

    def test_account_search_is_owner_only_bounded_and_marks_invited_users(self) -> None:
        from unittest.mock import Mock

        authenticator = Mock()
        self.app.config["CLERK_AUTHENTICATOR"] = authenticator
        for headers, status in (({}, 401), (self._bearer(self.alice_token), 403)):
            response = self.client.get("/api/v1/accounts?query=reader", headers=headers)
            self.assertEqual(response.status_code, status)
        authenticator.search_accounts.assert_not_called()
        owner = self._owner_token()
        headers = self._bearer(owner)
        for query in ({"query": "a"}, {"query": "a" * 101}, {"query": "reader", "offset": "invalid"}, {"query": "reader", "offset": "-1"}, {"query": "reader", "offset": "10001"}):
            self.assertEqual(self.client.get("/api/v1/accounts", query_string=query, headers=headers).status_code, 400)
        authenticator.search_accounts.return_value = {"accounts": [
            {"clerk_user_id": "user_owner", "name": "Owner", "email": "owner@example.test"},
            {"clerk_user_id": "user_reader", "name": "Reader", "email": "reader@example.test"},
        ], "next_offset": 20}
        response = self.client.get("/api/v1/accounts?query=reader&offset=20", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual([item["already_invited"] for item in response.get_json()["accounts"]], [True, False])
        authenticator.search_accounts.assert_called_once_with("reader", 20)

    def test_picker_invitation_binds_stable_account_and_enforces_scope(self) -> None:
        from accounts import link_clerk_account
        from unittest.mock import Mock

        owner = self._owner_token()
        authenticator = Mock()
        self.app.config["CLERK_AUTHENTICATOR"] = authenticator
        authenticator.get_account.return_value = {"clerk_user_id": "user_reader", "email": "reader@example.test", "name": "Reader"}
        payload = {"principal_id": "reader", "clerk_user_id": "user_reader", "access": "read-only", "readable_paths": ["visible/file.txt"]}
        response = self.client.post("/api/v1/invitations", json=payload, headers=self._bearer(owner))
        self.assertEqual(response.status_code, 201)
        authenticator.get_account.assert_called_once_with("user_reader")
        principal = load_state(self.forge)["principals"]["reader"]
        self.assertEqual(principal["clerk_user_id"], "user_reader")
        self.assertEqual(principal["emails"], ["reader@example.test"])
        self.assertEqual(link_clerk_account(self.forge, "user_reader", ["changed@example.test"]), "reader")
        token = add_token(self.forge, "reader")
        descriptor = self.client.get("/api/v1/session", headers=self._bearer(token)).get_json()["descriptor"]
        self.assertEqual(descriptor["capabilities"]["modify"], [])
        self.assertEqual(self.client.get("/api/v1/blob", query_string={"path_b64": self._b64(b"hidden/secret.txt")}, headers=self._bearer(token)).status_code, 404)
        self.assertEqual(self.client.post("/api/v1/invitations", json=payload | {"principal_id": "other"}, headers=self._bearer(owner)).status_code, 409)
        forged = payload | {"principal_id": "forged", "email": "forged@example.test"}
        self.assertEqual(self.client.post("/api/v1/invitations", json=forged, headers=self._bearer(owner)).status_code, 400)
        forge.check_invariants(self.forge)

    def test_picker_rechecks_unavailable_accounts_and_handles_directory_outages(self) -> None:
        from clerk_auth import ClerkDirectoryError
        from unittest.mock import Mock

        owner = self._owner_token()
        headers = self._bearer(owner)
        payload = {"principal_id": "reader", "clerk_user_id": "user_reader", "access": "read-only", "readable_paths": ["visible"]}
        self.assertEqual(self.client.get("/api/v1/accounts?query=reader", headers=headers).status_code, 404)
        self.assertEqual(self.client.post("/api/v1/invitations", json=payload, headers=headers).status_code, 404)
        authenticator = Mock()
        self.app.config["CLERK_AUTHENTICATOR"] = authenticator
        authenticator.get_account.return_value = None
        self.assertEqual(self.client.post("/api/v1/invitations", json=payload, headers=headers).get_json(), {"error": "account-unavailable"})
        authenticator.get_account.side_effect = ClerkDirectoryError("private details")
        self.assertEqual(self.client.post("/api/v1/invitations", json=payload, headers=headers).status_code, 503)
        authenticator.search_accounts.side_effect = ClerkDirectoryError("private details")
        response = self.client.get("/api/v1/accounts?query=reader", headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(b"private details", response.data)
        self.assertNotIn("reader", load_state(self.forge)["principals"])

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
