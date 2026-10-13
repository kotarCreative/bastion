"""Multi-repo host against real workerd SQL and authenticated Artifacts HTTPS."""
import base64
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from werkzeug.serving import make_server
from flask import Flask
from cloudflare_runtime import build_app
from clerk_auth import ClerkConfig
from errors import ForgeError
from object_store_artifacts import ArtifactsObjectStore
from repository import Repository, clear_repository_cache
from state import load_state
from tests.artifacts_https_harness import ArtifactsHttpsHarness
from tests.durable_objects_harness import AVAILABLE, DurableObjectsHarness


def git(path, *args, email="admin@example.test", check=True):
    env = dict(os.environ, GIT_AUTHOR_NAME="Test", GIT_COMMITTER_NAME="Test",
               GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_EMAIL=email, GIT_TERMINAL_PROMPT="0")
    result = subprocess.run(["git", "-C", str(path), *args], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
    if check and result.returncode:
        raise AssertionError(result.stderr.decode(errors="replace"))
    return result


class FakeClerk:
    def __init__(self, config):
        pass

    def authenticate(self, headers):
        user = headers["Authorization"].split(" ", 1)[1]
        return user, [self.get_account(user)["email"]]

    def get_account(self, user):
        email = {"user_admin": "admin@example.test", "user_bob": "bob@example.test"}.get(user)
        return {"clerk_user_id": user, "email": email} if email else None


@unittest.skipUnless(AVAILABLE, "install deployment/cloudflare dependencies for workerd")
class CloudflareHostTest(unittest.TestCase):
    def setUp(self):
        self.do = DurableObjectsHarness()
        self.addCleanup(self.do.close)
        self.remote = ArtifactsHttpsHarness()
        self.addCleanup(self.remote.close)
        self.temp = tempfile.TemporaryDirectory(prefix="bastion-multi-cf-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "host"
        env = patch.dict(os.environ, {
            "BASTION_CATALOG_ENDPOINT": self.do.endpoint, "BASTION_DATABASE": "durable_objects",
            "BASTION_ROOT": str(self.root), "BASTION_ARTIFACTS_REMOTE": self.remote.remote,
            "BASTION_ARTIFACTS_TOKEN": "test-backing-token", "BASTION_ARTIFACTS_CACHE": str(self.base / "cache"),
            "BASTION_BOOTSTRAP_ADMIN_EMAIL": "admin@example.test",
        })
        env.start(); self.addCleanup(env.stop)
        original = ArtifactsObjectStore.from_config.__func__
        def configured(cls, config, cache_dir):
            return self.remote.configure_cache(original(cls, config, cache_dir))
        adapter = patch.object(ArtifactsObjectStore, "from_config", new=classmethod(configured))
        adapter.start(); self.addCleanup(adapter.stop)
        config = ClerkConfig("pk_test", "sk_test", ("https://app.example.test",), "https://clerk.example.test")
        for mock in (patch("clerk_auth.load_clerk_config", return_value=config),
                     patch("clerk_auth.ClerkAuthenticator", FakeClerk)):
            mock.start(); self.addCleanup(mock.stop)
        self.runtime = build_app()
        self.host = self.runtime.host
        # Exercise the production serialized WSGI wrapper, not just Flask routes.
        self.client = Flask("host-test").test_client()
        self.client.application.wsgi_app = self.runtime
        self.auth = self.login("user_admin")

    def login(self, user):
        response = self.client.post("/api/v1/auth/clerk", json={}, headers={"Authorization": "Bearer " + user})
        self.assertEqual(200, response.status_code, response.json)
        return {"Authorization": "Bearer " + response.json["access_token"]}

    def create(self, name):
        response = self.client.post("/api/v1/repos", json={"name": name, "request_id": name}, headers=self.auth)
        self.assertEqual(201, response.status_code, response.json)
        return response.json["repo"]

    def exchange(self, repo, auth=None):
        response = self.client.post(f"/api/v1/repos/{repo['repo_id']}/auth/session", json={}, headers=auth or self.auth)
        self.assertEqual(200, response.status_code, response.json)
        return {"Authorization": "Bearer " + response.json["access_token"]}

    def credential(self, repo, ui, view="owner"):
        response = self.client.post(f"/api/v1/repos/{repo['repo_id']}/credentials",
            json={"view_id": view, "scopes": ["fetch", "push"], "expires_in": "1h"}, headers=ui)
        self.assertEqual(201, response.status_code, response.json)
        return response.json["credential"]

    def source(self):
        path = self.base / "source"
        path.mkdir(); git(path, "init", "-b", "main")
        (path / "src").mkdir(); (path / "src" / "code.txt").write_text("code\n")
        (path / "private.txt").write_text("private\n")
        git(path, "add", "."); git(path, "commit", "-m", "existing code", email="historical@example.test")
        return path

    def reserve(self, name="imported"):
        response = self.client.post("/api/v1/imports", json={"name": name, "branch": "main", "request_id": name}, headers=self.auth)
        self.assertEqual(201, response.status_code, response.json)
        op = response.json["import"]
        credential = self.client.post(f"/api/v1/imports/{op['import_id']}/credentials", json={}, headers=self.auth).json["credential"]
        return op, credential

    def restart(self):
        shutil.rmtree(self.root)
        shutil.rmtree(self.base / "cache", ignore_errors=True)
        clear_repository_cache()
        self.do.restart()
        self.runtime = build_app(); self.host = self.runtime.host
        self.client.application.wsgi_app = self.runtime

    def test_host_starts_empty_and_separates_repo_catalogs_objects_and_credentials(self):
        self.assertEqual([], self.host.list_repos())
        self.assertFalse(list(self.root.rglob("*.sqlite3")))
        self.assertEqual(200, self.client.get("/readyz").status_code)
        a, b = self.create("one"), self.create("two")
        ui = self.exchange(a)
        self.assertEqual(401, self.client.get(f"/api/v1/repos/{b['repo_id']}/tree", headers=ui).status_code)
        normal = self.credential(a, ui)
        from http_server import authenticate_token
        self.assertIsNone(authenticate_token(self.root / "repos" / b["repo_id"], normal))
        ra, rb = (Repository.open(self.root / "repos" / r["repo_id"]) for r in (a, b))
        self.assertNotEqual(ra.repository_id, rb.repository_id)
        self.assertNotEqual(ra.store.identity, rb.store.identity)
        self.restart()
        self.assertEqual(2, len(self.host.list_repos()))
        retried = self.create("one")
        self.assertEqual(a["repo_id"], retried["repo_id"])
        self.assertEqual(200, self.client.get(f"/api/v1/repos/{a['repo_id']}/tree", headers=ui).status_code)
        self.assertFalse(list(self.root.rglob("*.sqlite3")))

    def test_real_first_push_permissions_and_tokens_survive_container_and_do_replacement(self):
        op, credential = self.reserve()
        source = self.source()
        server = make_server("127.0.0.1", 0, self.runtime, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        url = f"http://127.0.0.1:{server.server_port}/admin/imported.git"
        header = "Authorization: Basic " + base64.b64encode(("owner:" + credential).encode()).decode()
        try:
            git(source, "-c", "http.extraHeader=" + header, "push", url, "main")
            repo = self.client.get(f"/api/v1/imports/{op['import_id']}", headers=self.auth).json["import"]["repo"]
            ui = self.exchange(repo)
            owner_token = self.credential(repo, ui)
            approval = self.client.post("/api/v1/host/accounts", headers=self.auth,
                json={"owner_name": "bob", "email": "bob@example.test"})
            self.assertEqual(201, approval.status_code, approval.json)
            bob_auth = self.login("user_bob")
            self.assertEqual(404, self.client.post("/api/v1/host/accounts", headers=bob_auth,
                json={"owner_name": "mallory", "email": "mallory@example.test"}).status_code)
            self.assertEqual([], self.client.get("/api/v1/repos", headers=bob_auth).json["repos"])
            invite = self.client.post(f"/api/v1/repos/{repo['repo_id']}/invitations", headers=ui,
                json={"principal_id": "bob", "clerk_user_id": "user_bob", "readable_paths": ["/"], "access": "contributor"})
            self.assertEqual(201, invite.status_code, invite.json)
            bob_ui = self.exchange(repo, bob_auth)
            bob_token = self.credential(repo, bob_ui, "bob")
            response = self.client.post(f"/api/v1/repos/{repo['repo_id']}/permissions", headers=ui,
                json={"view_id": "bob", "path_b64": base64.b64encode(b"private.txt").decode(), "private": True})
            self.assertEqual(200, response.status_code, response.json)
            clone = self.base / "bob-clone"
            bob_header = "Authorization: Basic " + base64.b64encode(("bob:" + bob_token).encode()).decode()
            git(self.base, "-c", "http.extraHeader=" + bob_header, "clone", url, str(clone))
            self.assertTrue((clone / "src" / "code.txt").exists())
            self.assertFalse((clone / "private.txt").exists())
            (clone / "src" / "new.txt").write_text("allowed\n")
            git(clone, "add", "."); git(clone, "commit", "-m", "allowed", email="bob@example.test")
            git(clone, "-c", "http.extraHeader=" + bob_header, "push", "origin", "main")
            (clone / "private.txt").write_text("forbidden\n")
            git(clone, "add", "."); git(clone, "commit", "-m", "denied", email="bob@example.test")
            self.assertNotEqual(0, git(clone, "-c", "http.extraHeader=" + bob_header, "push", "origin", "main", check=False).returncode)
            root = self.root / "repos" / repo["repo_id"]
            identity, tip = Repository.open(root).repository_id, Repository.open(root).load()[0].canonical_tip
        finally:
            server.shutdown(); thread.join(5); server.server_close()
        self.restart()
        self.assertEqual(200, self.client.get(f"/api/v1/repos/{repo['repo_id']}/tree", headers=bob_ui).status_code)
        restored = Repository.open(root)
        self.assertEqual(identity, restored.repository_id)
        self.assertEqual(tip, restored.load()[0].canonical_tip)
        self.assertEqual("user_admin", load_state(root)["principals"]["owner"]["clerk_user_id"])
        from http_server import authenticate_token
        self.assertIsNotNone(authenticate_token(root, owner_token))
        self.assertEqual(200, self.client.get("/api/v1/host/accounts", headers=self.auth).status_code)
        from invariants import check_invariants
        check_invariants(root)
        self.assertFalse(list(self.root.rglob("*.sqlite3")))

    def test_interrupted_import_recovers_exact_intake_without_reseeding(self):
        source = self.source()
        from tests.test_first_push_import import receive_body
        import first_push, repository_import
        account = self.host.authenticate_session(self.auth["Authorization"].split()[1])["account_id"]
        body = receive_body(source)
        for index, barrier in enumerate(("source_validated", "repo_ready_before_import_ready")):
            with self.subTest(barrier=barrier):
                op, _credential = self.reserve("import-" + str(index))
                row = repository_import.get(self.host, account, op["import_id"])
                def crash(stage):
                    if stage == barrier:
                        raise SystemExit("container replaced")
                with self.assertRaises(SystemExit):
                    first_push.receive(self.host, row, body, lambda: None, failpoint=crash)
                self.restart()
                current = repository_import.get(self.host, account, op["import_id"])
                completed = first_push.receive(self.host, current, body, lambda: None)
                self.assertEqual("ready", completed["state"])
                repo_id = completed["repo_id"]
                original_tip = Repository.open(self.root / "repos" / repo_id).load()[0].canonical_tip
                self.restart()
                current = repository_import.get(self.host, account, op["import_id"])
                retried = first_push.receive(self.host, current, body, lambda: None)
                self.assertEqual(repo_id, retried["repo_id"])
                self.assertEqual(original_tip, Repository.open(self.root / "repos" / repo_id).load()[0].canonical_tip)
        self.assertEqual(2, len(self.host.list_repos()))

    def test_catalog_outage_fails_closed_without_bootstrapping(self):
        self.do.stop()
        with self.assertRaises(ForgeError):
            build_app()
        self.assertEqual(503, self.client.get("/readyz").status_code)
        self.do.start()
        self.assertEqual([], self.host.list_repos())

    def test_interrupted_repository_creation_resumes_same_durable_identity(self):
        for stage in ("before_forge_init", "forge_initialized", "owner_provisioned"):
            with self.subTest(stage=stage):
                def crash(current):
                    if current == stage:
                        raise SystemExit("container replaced")
                with self.assertRaises(SystemExit):
                    self.host.create_repo("admin", stage.replace("_", "-"), request_id=stage,
                                          seed_empty=True, failpoint=crash)
                reserved = next(row for row in self.host.list_repos() if row["request_id"] == stage)
                catalog = self.host.client.scoped("repos/" + reserved["repo_id"]).status()["root"]
                self.restart()
                result = self.host.create_repo("admin", stage.replace("_", "-"), request_id=stage, seed_empty=True)
                self.assertEqual(reserved["repo_id"], result["repo_id"])
                if catalog:
                    import json
                    self.assertEqual(json.loads(catalog["repository"])["repository_id"], result["repository_id"])
                self.assertEqual("ready", result["state"])
