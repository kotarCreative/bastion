"""Spec 013 verification: digest tokens, scopes, and the demo API."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

import forge
import tokens as token_model
from demo_server import create_app
from http_server import add_token, authenticate_token, list_tokens, revoke_token
from store import open_store
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


class HostedApiTest(unittest.TestCase):
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
        weird = b"weird-\xff.txt".decode("utf-8", "surrogateescape")
        (self.source / "visible" / weird).write_text("weird bytes\n")
        (self.source / "hidden" / "secret.txt").write_text("secret 1\n")
        env = dict(os.environ, GIT_AUTHOR_DATE="1700000000 +0000",
                   GIT_COMMITTER_DATE="1700000000 +0000")
        git(self.source, "add", "-A")
        git(self.source, "commit", "-q", "-m", "initial mixed state", env=env)

        self.forge = self.root / "forge"
        forge.init_forge(self.forge, self.source, "main")
        identity_harness.add_view(self.forge, "alice", ["visible"], ["visible"], "main")
        identity_harness.add_view(self.forge, "bob", ["visible"], ["visible"], "main")

        os.environ["BASTION_DEMO_SECRET"] = "test-secret-013-do-not-use"
        self.app = create_app(self.forge)
        self.client = self.app.test_client()
        self.alice_token = add_token(self.forge, "alice")
        self.bob_token = add_token(self.forge, "bob")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    # -- token model --------------------------------------------------------

    def test_credential_grammar_and_digest(self) -> None:
        token_id, secret = token_model.split_credential(self.alice_token)
        self.assertEqual(len(token_id), 22)
        self.assertEqual(len(secret), 43)
        with self.assertRaises(Exception):
            token_model.split_credential("not-a-token")
        with self.assertRaises(Exception):
            token_model.split_credential("bst1.short.short")
        record = authenticate_token(self.forge, self.alice_token)
        assert record is not None
        self.assertEqual(
            record["secret_digest"],
            token_model.digest(token_id, secret),
        )
        self.assertEqual(len(record["secret_digest"]), 64)

    def test_raw_secrets_never_persisted(self) -> None:
        token_id = self.alice_token.split(".")[1]
        secret = self.alice_token.split(".")[2]
        store = open_store(self.forge)
        try:
            rows = store._fetchall("SELECT doc FROM http_tokens", ())
        finally:
            store.close()
        blob = json.dumps(rows)
        self.assertNotIn(secret, blob)
        self.assertNotIn(self.alice_token, blob)
        self.assertIn(token_id, blob)

    def test_issue_and_revoke_are_audited_without_secrets(self) -> None:
        credential = add_token(self.forge, "alice", scopes=["fetch", "push"])
        token_id = credential.split(".")[1]
        store = open_store(self.forge)
        try:
            entries = [
                entry for entry in store.recent_audit(50)
                if entry["kind"] in ("token.issue", "token.revoke")
            ]
        finally:
            store.close()
        issued = [e for e in entries if e["kind"] == "token.issue" and token_id in e["subject"]]
        self.assertEqual(len(issued), 1)
        self.assertNotIn(credential, issued[0]["detail"])
        self.assertNotIn(credential, issued[0]["subject"])
        self.assertTrue(revoke_token(self.forge, token_id))
        store = open_store(self.forge)
        try:
            entries = [
                entry for entry in store.recent_audit(50)
                if entry["kind"] == "token.revoke"
            ]
        finally:
            store.close()
        revoked = [e for e in entries if token_id in e["subject"]]
        self.assertEqual(len(revoked), 1)
        self.assertNotIn(credential, revoked[0]["detail"])

    def test_expiry_takes_effect(self) -> None:
        credential = add_token(self.forge, "alice", expires_in="1m")
        self.assertIsNotNone(authenticate_token(self.forge, credential))
        token_id = credential.split(".")[1]
        store = open_store(self.forge)
        try:
            record = store.load_http_token(token_id)
            assert record is not None
            record["expires_at"] = int(time.time()) - 1
            import json as _json

            from store import _checksum

            store.backend.begin(store.connection)
            try:
                store._execute(
                    "UPDATE http_tokens SET doc = ?, checksum = ? WHERE key = ?",
                    (_json.dumps(record, sort_keys=True), _checksum(record), token_id),
                )
                store.backend.commit(store.connection)
            except Exception:
                store.backend.rollback(store.connection)
                raise
        finally:
            store.close()
        self.assertIsNone(authenticate_token(self.forge, credential))
        response = self.client.get(
            "/api/v1/session", headers=self._bearer(credential)
        )
        self.assertEqual(response.status_code, 401)

    def test_list_tokens_never_prints_digests(self) -> None:
        for record in list_tokens(self.forge):
            self.assertNotIn("secret_digest", record)
            self.assertNotIn("secret", json.dumps(record).lower().replace("_", " "))

    def test_backup_contains_digests_not_raw_tokens(self) -> None:
        destination = self.root / "backup"
        from store import backup_forge

        backup_forge(self.forge, destination)
        blob = b""
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                blob += path.read_bytes()
        self.assertNotIn(self.alice_token.encode(), blob)
        self.assertNotIn(self.bob_token.encode(), blob)
        self.assertFalse((destination / "tokens.json").exists())

    def test_legacy_tokens_rejected(self) -> None:
        (self.forge / "tokens.json").write_text('{"x": "alice"}\n')
        with self.assertRaises(Exception):
            create_app(self.forge)

    # -- API auth ------------------------------------------------------------

    def test_missing_or_bad_credentials_share_one_401(self) -> None:
        first = self.client.get("/api/v1/session")
        second = self.client.get(
            "/api/v1/session", headers={"Authorization": "Bearer junk"}
        )
        third = self.client.get(
            "/api/v1/session", headers=self._bearer(self.alice_token[:-2] + "xx")
        )
        for response in (first, second, third):
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.get_json(), {"error": "auth-required"})
            self.assertIn("Bearer", response.headers.get("WWW-Authenticate", ""))

    def test_fetch_only_and_push_only_cannot_call_api(self) -> None:
        fetch_only = add_token(self.forge, "alice", scopes=["fetch"])
        push_only = add_token(self.forge, "alice", scopes=["push"])
        for credential in (fetch_only, push_only):
            response = self.client.get(
                "/api/v1/session", headers=self._bearer(credential)
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.get_json(), {"error": "forbidden"})

    def test_revoked_token_returns_401(self) -> None:
        credential = add_token(self.forge, "alice")
        self.assertEqual(
            self.client.get("/api/v1/session", headers=self._bearer(credential)).status_code,
            200,
        )
        revoke_token(self.forge, credential.split(".")[1])
        response = self.client.get("/api/v1/session", headers=self._bearer(credential))
        self.assertEqual(response.status_code, 401)

    # -- session ---------------------------------------------------------------

    def test_session_exact_shape(self) -> None:
        response = self.client.get("/api/v1/session", headers=self._bearer(self.alice_token))
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual(
            set(data),
            {"view_id", "principal", "team_ids", "role_ids", "binding_ids", "descriptor"},
        )
        self.assertEqual(data["view_id"], "alice")
        self.assertEqual(set(data["principal"]), {"id", "kind"})
        self.assertNotIn("emails", json.dumps(data))
        descriptor = data["descriptor"]
        self.assertEqual(
            set(descriptor),
            {"id", "generation", "projection_epoch", "projected_ref",
             "capabilities", "restrictions"},
        )
        capability = descriptor["capabilities"]["read"][0]
        self.assertEqual(set(capability), {"prefix_b64", "prefix_display"})

    def test_each_persona_sees_only_its_projection(self) -> None:
        alice = self.client.get(
            "/api/v1/tree", headers=self._bearer(self.alice_token)
        ).get_json()
        paths = [entry["path_display"] for entry in alice["entries"]]
        self.assertIn("visible", paths)
        self.assertNotIn("hidden", paths)
        history = self.client.get(
            "/api/v1/history", headers=self._bearer(self.alice_token)
        ).get_json()
        self.assertTrue(history["commits"])
        for commit in history["commits"]:
            self.assertEqual(
                set(commit),
                {"oid", "parent_oid", "author_name_b64", "author_name_display",
                 "author_email_b64", "author_email_display", "authored_at",
                 "authored_timezone", "committer_name_b64", "committer_name_display",
                 "committer_email_b64", "committer_email_display", "committed_at",
                 "committed_timezone", "message_b64", "message_display"},
            )

    # -- tree --------------------------------------------------------------------

    def test_tree_pagination_and_cursor_integrity(self) -> None:
        prefix = base64.urlsafe_b64encode(b"visible").decode().rstrip("=")
        first = self.client.get(
            f"/api/v1/tree?path_b64={prefix}&limit=1",
            headers=self._bearer(self.alice_token),
        )
        self.assertEqual(first.status_code, 200)
        data = first.get_json()
        self.assertEqual(len(data["entries"]), 1)
        self.assertIsNotNone(data["next_cursor"])
        self.assertIn("no-store", first.headers.get("Cache-Control", ""))
        entry = data["entries"][0]
        self.assertEqual(
            set(entry), {"path_b64", "path_display", "mode", "kind", "size"}
        )
        second = self.client.get(
            f"/api/v1/tree?path_b64={prefix}&limit=1&cursor={data['next_cursor']}",
            headers=self._bearer(self.alice_token),
        )
        self.assertEqual(second.status_code, 200)
        # A cursor minted for another view is rejected.
        foreign = self.client.get(
            f"/api/v1/tree?path_b64={prefix}&limit=1&cursor={data['next_cursor']}",
            headers=self._bearer(self.bob_token),
        )
        self.assertEqual(foreign.status_code, 400)
        # A tampered cursor is rejected.
        tampered = data["next_cursor"][:-2] + "xx"
        bad = self.client.get(
            f"/api/v1/tree?path_b64={prefix}&limit=1&cursor={tampered}",
            headers=self._bearer(self.alice_token),
        )
        self.assertEqual(bad.status_code, 400)

    def test_hidden_and_unknown_prefixes_share_404(self) -> None:
        hidden_b64 = base64.urlsafe_b64encode(b"hidden").decode().rstrip("=")
        missing_b64 = base64.urlsafe_b64encode(b"nope").decode().rstrip("=")
        hidden = self.client.get(
            f"/api/v1/tree?path_b64={hidden_b64}", headers=self._bearer(self.alice_token)
        )
        missing = self.client.get(
            f"/api/v1/tree?path_b64={missing_b64}", headers=self._bearer(self.alice_token)
        )
        self.assertEqual(hidden.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(hidden.get_json(), missing.get_json())

    def test_non_utf8_path_round_trips(self) -> None:
        encoded = base64.urlsafe_b64encode(b"visible/weird-\xff.txt").decode().rstrip("=")
        response = self.client.get(
            "/api/v1/tree?path_b64=" + base64.urlsafe_b64encode(b"visible").decode().rstrip("="),
            headers=self._bearer(self.alice_token),
        )
        self.assertEqual(response.status_code, 200)
        matches = [
            entry for entry in response.get_json()["entries"]
            if entry["path_b64"] == encoded
        ]
        self.assertEqual(len(matches), 1)
        self.assertNotEqual(matches[0]["path_display"], "")

    # -- decisions ---------------------------------------------------------------

    def test_decisions_evaluate_policy_only(self) -> None:
        def decide(path: bytes):
            body = {
                "capability": "read",
                "path_b64": base64.urlsafe_b64encode(path).decode().rstrip("="),
            }
            return self.client.post(
                "/api/v1/decisions", json=body, headers=self._bearer(self.alice_token)
            )

        allowed = decide(b"visible/file.txt")
        self.assertEqual(allowed.status_code, 200)
        self.assertTrue(allowed.get_json()["allowed"])
        self.assertIsNotNone(allowed.get_json()["longest_allow"])
        denied = decide(b"hidden/secret.txt")
        self.assertEqual(denied.status_code, 200)
        self.assertFalse(denied.get_json()["allowed"])
        # Extra fields and unknown capabilities are rejected.
        extra = self.client.post(
            "/api/v1/decisions",
            json={"capability": "read", "path_b64": "eA", "other": 1},
            headers=self._bearer(self.alice_token),
        )
        self.assertEqual(extra.status_code, 400)
        unknown = self.client.post(
            "/api/v1/decisions",
            json={"capability": "admin-all", "path_b64": "eA"},
            headers=self._bearer(self.alice_token),
        )
        self.assertEqual(unknown.status_code, 400)

    # -- health --------------------------------------------------------------------

    def test_health_and_readiness(self) -> None:
        health = self.client.get("/healthz")
        self.assertEqual(health.status_code, 200)
        ready = self.client.get("/readyz")
        self.assertEqual(ready.status_code, 200)
        self.assertEqual(ready.get_json(), {"status": "ok"})
        unknown = self.client.get("/api/v1/nope", headers=self._bearer(self.alice_token))
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(unknown.get_json(), {"error": "not-found"})


if __name__ == "__main__":
    unittest.main()
