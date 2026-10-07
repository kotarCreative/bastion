"""Production Python backend and cold recovery against workerd's DO SQL."""

from __future__ import annotations

import json
import base64
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cloudflare_runtime
from accounts import link_clerk_account
from durable_store import DurableObjectsBackend, StagedTransaction
from errors import ForgeError
from http_server import add_token
from object_store_artifacts import ArtifactsObjectStore
from repository import Repository, clear_repository_cache
from state import load_state
from store import Store, initialize_store, load_forge_meta_readonly, load_principals_readonly, open_store
from tests.artifacts_https_harness import ArtifactsHttpsHarness
from tests.durable_objects_harness import AVAILABLE, DurableObjectsHarness


@unittest.skipUnless(AVAILABLE, "run npm ci in deployment/cloudflare to install the Workers test runtime")
class DurableObjectsTest(unittest.TestCase):
    def setUp(self):
        self.runtime = DurableObjectsHarness()
        self.addCleanup(self.runtime.close)
        self.temporary = tempfile.TemporaryDirectory(prefix="bastion-do-python-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "root"
        self.environment = patch.dict(os.environ, {
            "BASTION_CATALOG_ENDPOINT": self.runtime.endpoint,
            "BASTION_DATABASE": "durable_objects",
            "BASTION_ROOT": str(self.root),
            "BASTION_ARTIFACTS_CACHE": str(self.base / "cache"),
            "BASTION_BOOTSTRAP_ADMIN_EMAIL": "admin@example.test",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def test_backend_transaction_fencing_rollback_and_process_restart(self):
        self.root.mkdir()
        store = initialize_store(self.root)
        self.addCleanup(store.close)
        self.assertEqual("durable_objects", store.backend.kind)
        self.assertFalse((self.root / "store.sqlite3").exists())
        backend = store.backend
        connection = store.connection
        backend.begin_write(connection)
        connection.execute("INSERT INTO teams VALUES (?, ?, ?, ?)", ("test", "{}", 1, "checksum"))
        self.assertEqual([["test"]], connection.execute("SELECT key FROM teams").fetchall())
        backend.rollback(connection)
        self.assertEqual([], connection.execute("SELECT key FROM teams").fetchall())

        second = Store(DurableObjectsBackend(self.root))
        self.addCleanup(second.close)
        backend.begin_write(connection)
        connection.execute("INSERT INTO teams VALUES ('persisted', '{}', 1, 'checksum')")
        second.backend.begin_read(second.connection)
        backend.commit(connection)
        # Simulate the commit response disappearing after the DO persisted it.
        # CatalogClient must retry the exact operation without duplicate rows.
        original_open = backend.client.opener.open
        dropped = [False]

        class LostResponse:
            def __init__(self, response):
                self.response = response

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.response.close()

            def read(self, _limit):
                raise OSError("injected lost response after commit")

        def lose_commit_response(request, **kwargs):
            response = original_open(request, **kwargs)
            if request.full_url.endswith("/commit") and not dropped[0]:
                dropped[0] = True
                return LostResponse(response)
            return response

        backend.begin_write(connection)
        connection.execute("INSERT INTO teams VALUES ('retry-once', '{}', 1, 'checksum')")
        with patch.object(backend.client.opener, "open", side_effect=lose_commit_response):
            backend.commit(connection)
        self.assertTrue(dropped[0])
        with self.assertRaisesRegex(ForgeError, "revision conflict"):
            second.connection.execute("SELECT key FROM teams")
        second.backend.rollback(second.connection)
        self.runtime.restart()
        self.assertEqual([["persisted"], ["retry-once"]], connection.execute("SELECT key FROM teams ORDER BY key").fetchall())
        self.assertIsNone(load_forge_meta_readonly(self.root)["repository_id"])
        self.assertEqual({}, load_principals_readonly(self.root))

    def test_staged_transaction_survives_restart_and_normal_backend_rollback(self):
        self.root.mkdir()
        store = initialize_store(self.root)
        self.addCleanup(store.close)
        backend, connection = store.backend, store.connection
        with patch("durable_store.STAGE_THRESHOLD", 100):
            backend.begin_write(connection)
            connection.execute("INSERT INTO teams VALUES (?, ?, ?, ?)", ("staged", "{}", 1, "checksum"))
            self.assertEqual([["staged"]], connection.execute("SELECT key FROM teams").fetchall())
            self.assertIsNotNone(connection.staged)
            self.runtime.restart()
            self.assertEqual([["staged"]], connection.execute("SELECT key FROM teams").fetchall())
            backend.rollback(connection)
            self.assertEqual([], connection.execute("SELECT key FROM teams").fetchall())
            backend.begin_write(connection)
            connection.execute("INSERT INTO teams VALUES ('kept', '{}', 1, 'checksum')")
            connection.execute("INSERT INTO teams VALUES ('also-kept', '{}', 1, 'checksum')")
            self.assertIsNotNone(connection.staged)
            backend.commit(connection)
        self.runtime.restart()
        self.assertEqual([["also-kept"], ["kept"]], connection.execute("SELECT key FROM teams ORDER BY key").fetchall())

    def test_legacy_catalog_larger_than_one_request_imports_and_exports(self):
        from cloudflare_durable_runtime import import_snapshot
        from object_store_memory import MemoryObjectStore
        from store import _checksum

        source = self.base / "legacy"
        source.mkdir()
        store = initialize_store(source, database="sqlite")
        store.close()
        rows = []
        for index in range(100):
            document = {"id": f"person-{index:03}", "description": "persistent é漢字 " + "x" * 100_000}
            rows.append((document["id"], json.dumps(document, ensure_ascii=False), 1, _checksum(document)))
        with sqlite3.connect(source / "store.sqlite3") as connection:
            connection.executemany("INSERT INTO principals VALUES (?, ?, ?, ?)", rows)
        (source / "repository.json").write_text('{"repository_id":"large-import"}')
        artifacts = MemoryObjectStore()
        cloudflare_runtime.checkpoint_root(source, artifacts)
        payload = cloudflare_runtime._read_snapshot(artifacts)[0]
        self.assertGreater((source / "store.sqlite3").stat().st_size, 8 * 1024 * 1024)
        import_snapshot(self.runtime.client, payload)
        self.assertEqual("large-import", json.loads(self.runtime.client.status()["root"]["repository"])["repository_id"])
        self.runtime.restart()
        backend = DurableObjectsBackend(self.root)
        store = Store(backend)
        try:
            self.assertEqual(100, len(store.load_sections(["principals"])["principals"]))
        finally:
            store.close()
        exported = self.base / "large-export.sqlite3"
        backend.dump(exported)
        with sqlite3.connect(exported) as connection:
            self.assertEqual(rows, connection.execute("SELECT key, doc, version, checksum FROM principals ORDER BY key").fetchall())
        self.assertGreater(exported.stat().st_size, 8 * 1024 * 1024)

        # A live mutation between pages must fail the export, rather than
        # return a backup composed of different catalog revisions.
        original_call = backend.client.call
        mutated = [False]

        def change_between_pages(path, body):
            result = original_call(path, body)
            if path == "/export" and body["table"] == "principals" and not mutated[0]:
                mutated[0] = True
                backend.client.commit(body["revision"], [{"sql": "UPDATE principals SET version = version + 1 WHERE key = ?", "params": ["person-000"]}])
            return result

        incomplete = self.base / "inconsistent-export.sqlite3"
        with patch.object(backend.client, "call", side_effect=change_between_pages):
            with self.assertRaisesRegex(ForgeError, "revision conflict"):
                backend.dump(incomplete)
        self.assertFalse(incomplete.exists())

    def test_staged_frames_split_unicode_and_retry_lost_stage_and_commit_responses(self):
        transaction = StagedTransaction(self.runtime.client, 0)
        original_open = self.runtime.client.opener.open
        dropped = set()

        class LostResponse:
            def __init__(self, response):
                self.response = response

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.response.close()

            def read(self, _limit):
                raise OSError("injected lost staged transaction response")

        def lose_response(request, **kwargs):
            response = original_open(request, **kwargs)
            for path in ("/stage", "/commit"):
                if request.full_url.endswith(path) and path not in dropped:
                    dropped.add(path)
                    return LostResponse(response)
            return response

        value = "é漢字🦊" * 40
        with patch("durable_store.FRAME_BYTES", 43), patch.object(self.runtime.client.opener, "open", side_effect=lose_response):
            transaction.append([
                {"sql": "CREATE TABLE teams (key TEXT PRIMARY KEY, doc TEXT)", "params": []},
                {"sql": "INSERT INTO teams VALUES (?, ?)", "params": ["unicode", value]},
            ])
            self.assertEqual(0, self.runtime.client.status()["revision"])
            self.runtime.restart()
            self.assertEqual(1, transaction.commit([], root={"repository": "{}", "phase": "ready"}))
        self.assertEqual({"/stage", "/commit"}, dropped)
        result = self.runtime.client.call("/query", {"revision": 1, "statement": {"sql": "SELECT doc FROM teams", "params": []}})
        self.assertEqual([[value]], result["rows"])

    def _artifacts(self):
        remote = ArtifactsHttpsHarness()
        self.addCleanup(remote.close)
        original = ArtifactsObjectStore.from_config.__func__

        def configured(cls, config, cache_dir):
            return remote.configure_cache(original(cls, config, cache_dir))

        env = patch.dict(os.environ, {"BASTION_ARTIFACTS_REMOTE": remote.remote, "BASTION_ARTIFACTS_TOKEN": "test-backing-token"})
        env.start()
        self.addCleanup(env.stop)
        adapter = patch.object(ArtifactsObjectStore, "from_config", new=classmethod(configured))
        adapter.start()
        self.addCleanup(adapter.stop)
        return remote

    def test_fresh_bootstrap_tokens_and_clerk_link_survive_disk_and_do_restart(self):
        self._artifacts()
        app = cloudflare_runtime.build_app()
        self.assertFalse((self.root / "store.sqlite3").exists())
        self.assertEqual("durable_objects", json.loads((self.root / "backend.json").read_text())["backend"])
        repository_id = Repository.open(self.root).repository_id
        token = add_token(self.root, "admin", scopes=["fetch", "push", "ui"])
        link_clerk_account(self.root, "user_durableAdmin", ["admin@example.test"])
        from tests.test_cloudflare_runtime_integration import HttpServer, _http
        server = HttpServer(app)
        self.addCleanup(server.close)
        self.assertEqual(200, _http(server, "/api/v1/session", token=token)[0])
        # Exercise the receive hook subprocess with the real DO adapter, then
        # a permission epoch that must survive both kinds of cold restart.
        from tests.test_cloudflare_runtime_integration import _run
        from identity import CAPABILITIES
        from service import add_principal, add_role, add_view, bind_role, managed_private_paths
        import forge
        clone = self.base / "clone"
        authenticated = server.base.replace("http://", f"http://admin:{token}@") + "/admin.git"
        _run("git", "clone", authenticated, str(clone))
        (clone / "private.txt").write_text("must remain hidden from bob\n")
        _run("git", "-C", str(clone), "add", "private.txt")
        commit_env = dict(os.environ, GIT_AUTHOR_NAME="DO Test", GIT_AUTHOR_EMAIL="admin@example.test", GIT_COMMITTER_NAME="DO Test", GIT_COMMITTER_EMAIL="admin@example.test")
        _run("git", "-C", str(clone), "commit", "-m", "Persist DO-backed push", env=commit_env)
        _run("git", "-C", str(clone), "push", authenticated, "HEAD:main")
        add_principal(self.root, "bob", ["bob@example.invalid"])
        allow = {capability: [] for capability in CAPABILITIES}
        allow["read"] = [""]
        add_role(self.root, "bob-reader", allow, {capability: [] for capability in CAPABILITIES})
        bind_role(self.root, "bob-reader", "principal", "bob")
        add_view(self.root, "bob", "bob", "main")
        bob_token = add_token(self.root, "bob", scopes=["fetch", "ui"])
        response = _http(server, "/api/v1/permissions", method="POST", token=token, body={"view_id": "bob", "path_b64": base64.b64encode(b"private.txt").decode(), "private": True})
        self.assertEqual(200, response[0], response[1])
        self.assertEqual(["private.txt"], managed_private_paths(load_state(self.root), "bob"))
        bob_clone = self.base / "bob-clone"
        bob_authenticated = server.base.replace("http://", f"http://bob:{bob_token}@") + "/bob.git"
        _run("git", "clone", bob_authenticated, str(bob_clone))
        self.assertFalse((bob_clone / "private.txt").exists())
        pushed_tip = Repository.open(self.root).load()[0].canonical_tip

        # The established backup format remains portable to a SQLite host.
        from store import backup_forge, restore_forge
        backup = backup_forge(self.root, self.base / "backup")
        restored = restore_forge(backup, self.base / "portable-restored", database="sqlite")
        self.assertEqual(
            "user_durableAdmin",
            load_state(restored)["principals"]["admin"]["clerk_user_id"],
        )
        self.assertEqual(repository_id, Repository.open(restored).repository_id)
        server.close()
        # Neither repository caches nor the Container filesystem carry durable
        # catalog state. Restart workerd too to evict all DO in-memory state.
        shutil.rmtree(self.root)
        shutil.rmtree(self.base / "cache", ignore_errors=True)
        clear_repository_cache()
        self.runtime.restart()
        recovered = cloudflare_runtime.build_app()
        self.assertEqual(repository_id, Repository.open(self.root).repository_id)
        self.assertFalse((self.root / "store.sqlite3").exists())
        self.assertEqual(
            "user_durableAdmin",
            load_state(self.root)["principals"]["admin"]["clerk_user_id"],
        )
        self.assertEqual("ready", load_state(self.root)["repository_state"])
        self.assertEqual(pushed_tip, Repository.open(self.root).load()[0].canonical_tip)
        self.assertEqual(["private.txt"], managed_private_paths(load_state(self.root), "bob"))
        server = HttpServer(recovered)
        self.addCleanup(server.close)
        self.assertEqual(200, _http(server, "/api/v1/session", token=token)[0])
        _run("git", "-C", str(bob_clone), "fetch", server.base.replace("http://", f"http://bob:{bob_token}@") + "/bob.git")
        self.assertNotEqual(0, forge.git(bob_clone, "cat-file", "-e", "FETCH_HEAD:private.txt", check=False).returncode)
        from object_store import ObjectNotFound
        with self.assertRaises(ObjectNotFound):
            cloudflare_runtime.build_store().get(cloudflare_runtime.CONTROL_KEY)
        self.runtime.stop()
        with patch.object(cloudflare_runtime, "bootstrap_root", side_effect=AssertionError("bootstrapped during outage")):
            with self.assertRaisesRegex(ForgeError, "storage unavailable"):
                cloudflare_runtime.build_app()
        self.runtime.start()
        self.assertEqual(
            "user_durableAdmin",
            load_state(self.root)["principals"]["admin"]["clerk_user_id"],
        )

    def test_interrupted_bootstrap_resumes_same_catalog_after_disk_loss(self):
        self._artifacts()
        import service
        original = service.add_principal

        def interrupted(root, principal, emails=None):
            original(root, principal, emails)
            raise RuntimeError("injected crash after durable principal commit")

        with patch.object(service, "add_principal", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                cloudflare_runtime.build_app()
        original_id = Repository.open(self.root).repository_id
        self.assertEqual("bootstrapping", self.runtime.client.status()["root"]["phase"])
        shutil.rmtree(self.root)
        shutil.rmtree(self.base / "cache", ignore_errors=True)
        clear_repository_cache()
        self.runtime.restart()
        cloudflare_runtime.build_app()
        self.assertEqual(original_id, Repository.open(self.root).repository_id)
        self.assertEqual(
            ["admin@example.test"],
            load_state(self.root)["principals"]["admin"]["emails"],
        )
        self.assertFalse((self.root / "store.sqlite3").exists())

    def test_interrupted_schema_initialization_resumes_without_resetting_data(self):
        self._artifacts()
        import store as store_module
        original = store_module.initialize_store

        def interrupted(*args, **kwargs):
            store = original(*args, **kwargs)
            store.close()
            raise RuntimeError("injected crash after schema commit")

        with patch.object(store_module, "initialize_store", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                cloudflare_runtime.build_app()
        self.assertIsNone(self.runtime.client.status()["root"])
        # Retry with the same disk as a Gunicorn startup retry would do.
        cloudflare_runtime.build_app()
        self.assertEqual(
            ["admin@example.test"],
            load_state(self.root)["principals"]["admin"]["emails"],
        )
        self.assertFalse((self.root / "store.sqlite3").exists())

    def test_legacy_snapshot_import_preserves_identity_links_and_tokens(self):
        self._artifacts()
        with patch.dict(os.environ, {"BASTION_DATABASE": "sqlite"}):
            cloudflare_runtime.build_app()
            repository_id = Repository.open(self.root).repository_id
            link_clerk_account(self.root, "user_importAdmin", ["admin@example.test"])
            token = add_token(self.root, "admin", scopes=["fetch", "ui"])
            cloudflare_runtime.checkpoint_root(self.root, cloudflare_runtime.build_store(), cloudflare_runtime._read_snapshot(cloudflare_runtime.build_store())[1])
        shutil.rmtree(self.root)
        clear_repository_cache()
        app = cloudflare_runtime.build_app()
        self.assertFalse((self.root / "store.sqlite3").exists())
        self.assertEqual(repository_id, Repository.open(self.root).repository_id)
        self.assertEqual(
            "user_importAdmin",
            load_state(self.root)["principals"]["admin"]["clerk_user_id"],
        )
        from tests.test_cloudflare_runtime_integration import HttpServer, _http
        server = HttpServer(app)
        self.addCleanup(server.close)
        self.assertEqual(200, _http(server, "/api/v1/session", token=token)[0])
        # The old checkpoint is retained for backup, but is never consulted
        # again once the Durable Object has its root configuration.
        with patch.object(cloudflare_runtime, "_read_snapshot", side_effect=AssertionError("legacy snapshot reused")):
            self.runtime.restart()
            cloudflare_runtime.build_app()
        store = open_store(self.root)
        try:
            store.audit("test", "import", "index preserved", event_id="unique-import-event")
            with self.assertRaises(ForgeError):
                store.audit("test", "import", "duplicate", event_id="unique-import-event")
        finally:
            store.close()
