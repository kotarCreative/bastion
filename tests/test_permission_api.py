"""Served scoped-policy compatibility and authorization tests."""
from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

import forge
from http_server import add_token
from permissions import empty_permissions
from repository_server import create_app
from service import add_role, bind_role, transition_principal
from tests import identity_harness


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                   env=os.environ | {"GIT_TERMINAL_PROMPT": "0"})


class PermissionApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "forge"
        source = Path(self.tmp.name) / "source"
        run("git", "init", "-q", "-b", "main", str(source))
        run("git", "-C", str(source), "config", "user.name", "Test")
        run("git", "-C", str(source), "config", "user.email", "test@example.test")
        (source / "file.txt").write_text("fixture\n")
        run("git", "-C", str(source), "add", ".")
        run("git", "-C", str(source), "commit", "-qm", "initial")
        forge.init_forge(self.root, source, "main")
        identity_harness.add_view(self.root, "alice", [""], [""], "main")
        identity_harness.add_view(self.root, "bob", [""], [""], "main")
        self.alice = add_token(self.root, "alice", scopes=["ui"])
        self.bob = add_token(self.root, "bob", scopes=["ui"])
        self.fetch_only = add_token(self.root, "alice", scopes=["fetch"])
        self.client = create_app(self.root).test_client()

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def auth(token):
        return {"Authorization": f"Bearer {token}"}

    def check(self, token, payload, **kwargs):
        return self.client.post("/api/v1/self/permissions/check", json=payload,
                                headers=self.auth(token), **kwargs)

    def activate(self, role, permissions):
        allow = {key: [] for key in ("read", "create", "modify", "delete", "mode",
                                     "symlink", "branch", "merge", "admin", "tag")}
        deny = dict(allow)
        add_role(self.root, role, allow, deny, permissions=permissions)
        bind_role(self.root, role, "principal", "alice")
        transition_principal(self.root, "alice")

    def test_v3_session_shape_and_default_deny_checks(self):
        response = self.client.get("/api/v1/session", headers=self.auth(self.alice))
        self.assertEqual(response.status_code, 200)
        descriptor = response.json["descriptor"]
        self.assertEqual(set(descriptor), {"id", "generation", "projection_epoch",
                                           "projected_ref", "capabilities", "restrictions"})
        self.assertEqual(self.check(self.alice, {"scope": "repository", "action": "pr_view"}).json,
                         {"allowed": False})
        self.assertEqual(self.check(self.alice, {"scope": "branch", "action": "read",
                                                 "ref": "refs/heads/main"}).json,
                         {"allowed": False})

    def test_v4_active_policy_visible_and_checked_via_existing_ui_token(self):
        policy = empty_permissions()
        policy["repository"]["allow"] = ["pr_view"]
        policy["branches"]["allow"]["read"] = ["refs/heads/feature/*"]
        self.activate("scoped-grant", policy)
        session = self.client.get("/api/v1/session", headers=self.auth(self.alice))
        self.assertEqual(session.status_code, 200)
        self.assertEqual(session.json["descriptor"]["permissions"], policy)
        # A v4 branch grant names a namespace, not an implicit grant to the
        # configured default branch. This fixture has only main, so projection
        # and public ref inventory are empty while the PR policy/session remain
        # available.
        self.assertIsNone(session.json["descriptor"]["projected_ref"])
        self.assertEqual(self.check(self.alice, {"scope": "repository", "action": "pr_view"}).json,
                         {"allowed": True})
        self.assertEqual(self.check(self.alice, {"scope": "branch", "action": "read",
                                                 "ref": "refs/heads/feature/demo"}).json,
                         {"allowed": True})
        self.assertEqual(self.check(self.alice, {"scope": "branch", "action": "read",
                                                 "ref": "refs/heads/not-feature"}).json,
                         {"allowed": False})
        refs = subprocess.run(["git", "--git-dir", str(self.root / "views" / "alice.git"),
                               "for-each-ref", "--format=%(refname)"], check=True,
                              text=True, stdout=subprocess.PIPE).stdout.splitlines()
        self.assertEqual(refs, [])
        self.assertEqual(self.client.get("/api/v1/tree", headers=self.auth(self.alice)).status_code, 404)

    def test_catalog_grant_dormant_then_revocation_updates_old_ui_token(self):
        grant = empty_permissions()
        grant["repository"]["allow"] = ["pr_create"]
        allow = {key: [] for key in ("read", "create", "modify", "delete", "mode",
                                     "symlink", "branch", "merge", "admin", "tag")}
        add_role(self.root, "dormant", allow, dict(allow), permissions=grant)
        bind_role(self.root, "dormant", "principal", "alice")
        self.assertFalse(self.check(self.alice, {"scope": "repository", "action": "pr_create"}).json["allowed"])
        transition_principal(self.root, "alice")
        self.assertTrue(self.check(self.alice, {"scope": "repository", "action": "pr_create"}).json["allowed"])
        denied = empty_permissions()
        denied["repository"]["deny"] = ["pr_create"]
        self.activate("revoke", denied)
        self.assertFalse(self.check(self.alice, {"scope": "repository", "action": "pr_create"}).json["allowed"])

    def test_no_implicit_admin_path_or_owner_permissions(self):
        # Fixture has path read/write but no scoped policy; neither those grants
        # nor owner-style root admin grants synthesize PR or branch rights.
        self.assertFalse(self.check(self.alice, {"scope": "repository", "action": "pr_view"}).json["allowed"])
        self.assertFalse(self.check(self.alice, {"scope": "branch", "action": "read",
                                                 "ref": "refs/heads/main"}).json["allowed"])

    def test_auth_shape_bounds_and_no_branch_existence_oracle(self):
        route = "/api/v1/self/permissions/check"
        self.assertEqual(self.client.post(route, json={"scope": "repository", "action": "pr_view"}).status_code, 401)
        self.assertEqual(self.check(self.fetch_only, {"scope": "repository", "action": "pr_view"}).status_code, 403)
        self.assertEqual(self.check(self.bob, {"scope": "repository", "action": "pr_view"}).status_code, 200)
        bad = [
            {"scope": "repository", "action": "pr_view", "extra": 1},
            {"scope": "repository", "action": "nope"},
            {"scope": "repository", "action": 1},
            {"scope": "branch", "action": "read"},
            {"scope": "branch", "action": "read", "ref": "main"},
            {"scope": "branch", "action": "read", "ref": "refs/heads/a*"},
            {"scope": "branch", "action": "delete", "ref": []},
            {"scope": "wat", "action": "read"},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                response = self.check(self.alice, payload)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json, {"error": "bad-request"})
                self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.activate("branch-grant", (lambda p: (p["branches"]["allow"]["read"].append(
            "refs/heads/feature/*"), p)[1])(empty_permissions()))
        unknown = self.check(self.alice, {"scope": "branch", "action": "read",
                                          "ref": "refs/heads/feature/no-such-ref"})
        existing = self.check(self.alice, {"scope": "branch", "action": "read",
                                           "ref": "refs/heads/feature/existing"})
        self.assertEqual(unknown.json, existing.json)
        oversized = self.client.post(route, data=b" " * (64 * 1024), content_type="application/json",
                                     headers=self.auth(self.alice))
        self.assertIn(oversized.status_code, (400, 413))
        self.assertEqual(oversized.headers["Cache-Control"], "no-store")


if __name__ == "__main__":
    unittest.main()
