"""Production DO SQL bridge, transaction recovery, and portable migration."""
import base64
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from durable_store import DurableObjectsBackend, StagedTransaction
from errors import ForgeError
from store import Store, initialize_store
from tests.durable_objects_harness import AVAILABLE, DurableObjectsHarness


@unittest.skipUnless(AVAILABLE, "install deployment/cloudflare dependencies for workerd")
class DurableObjectsTest(unittest.TestCase):
    def setUp(self):
        self.runtime = DurableObjectsHarness(); self.addCleanup(self.runtime.close)
        self.temp = tempfile.TemporaryDirectory(prefix="bastion-catalog-"); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"; self.root.mkdir()
        environment = patch.dict(os.environ, {"BASTION_CATALOG_ENDPOINT": self.runtime.endpoint, "BASTION_DATABASE": "durable_objects"})
        environment.start(); self.addCleanup(environment.stop)

    def test_revision_fencing_and_rollback_survive_runtime_restart(self):
        store = initialize_store(self.root); self.addCleanup(store.close)
        self.assertFalse((self.root / "store.sqlite3").exists())
        backend, connection = store.backend, store.connection
        backend.begin_write(connection)
        connection.execute("INSERT INTO teams VALUES ('temporary', '{}', 1, 'checksum')")
        self.assertEqual([["temporary"]], connection.execute("SELECT key FROM teams").fetchall())
        backend.rollback(connection)
        self.assertEqual([], connection.execute("SELECT key FROM teams").fetchall())
        other = Store(DurableObjectsBackend(self.root)); self.addCleanup(other.close)
        other.backend.begin_read(other.connection)
        backend.begin_write(connection)
        connection.execute("INSERT INTO teams VALUES ('persisted', '{}', 1, 'checksum')")
        backend.commit(connection)
        with self.assertRaisesRegex(ForgeError, "revision conflict"):
            other.connection.execute("SELECT key FROM teams")
        other.backend.rollback(other.connection)
        self.runtime.restart()
        self.assertEqual([["persisted"]], connection.execute("SELECT key FROM teams").fetchall())

    def test_staged_tentative_writes_rollback_and_commit(self):
        store = initialize_store(self.root); self.addCleanup(store.close)
        backend, connection = store.backend, store.connection
        with patch("durable_store.STAGE_THRESHOLD", 100):
            backend.begin_write(connection)
            connection.execute("INSERT INTO teams VALUES ('staged', '{}', 1, 'checksum')")
            self.assertEqual([["staged"]], connection.execute("SELECT key FROM teams").fetchall())
            self.assertIsNotNone(connection.staged)
            self.runtime.restart()
            self.assertEqual([["staged"]], connection.execute("SELECT key FROM teams").fetchall())
            backend.rollback(connection)
            self.assertEqual([], connection.execute("SELECT key FROM teams").fetchall())
            backend.begin_write(connection)
            connection.execute("INSERT INTO teams VALUES ('kept', '{}', 1, 'checksum')")
            connection.execute("INSERT INTO teams VALUES ('also-kept', '{}', 1, 'checksum')")
            backend.commit(connection)
        self.runtime.restart()
        self.assertEqual([["also-kept"], ["kept"]], connection.execute("SELECT key FROM teams ORDER BY key").fetchall())

    def test_lost_stage_and_commit_responses_are_idempotent_with_split_unicode(self):
        original = self.runtime.client.opener.open
        dropped = set()
        class LostResponse:
            def __init__(self, response): self.response = response
            def __enter__(self): return self
            def __exit__(self, *args): self.response.close()
            def read(self, limit): raise OSError("lost response after durable write")
        def lose(request, **kwargs):
            response = original(request, **kwargs)
            for path in ("/stage", "/commit"):
                if request.full_url.endswith(path) and path not in dropped:
                    dropped.add(path); return LostResponse(response)
            return response
        value = "é漢字🦊" * 40
        transaction = StagedTransaction(self.runtime.client, 0)
        with patch("durable_store.FRAME_BYTES", 43), patch.object(self.runtime.client.opener, "open", side_effect=lose):
            transaction.append([
                {"sql": "CREATE TABLE teams (key TEXT PRIMARY KEY, doc TEXT)", "params": []},
                {"sql": "INSERT INTO teams VALUES (?, ?)", "params": ["unicode", value]},
            ])
            self.assertEqual(0, self.runtime.client.status()["revision"])
            self.runtime.restart()
            self.assertEqual(1, transaction.commit([]))
        self.assertEqual({"/stage", "/commit"}, dropped)
        result = self.runtime.client.call("/query", {"revision": 1, "statement": {"sql": "SELECT doc FROM teams", "params": []}})
        self.assertEqual([[value]], result["rows"])

    def test_large_portable_catalog_import_export_and_consistency_fence(self):
        from cloudflare_durable_runtime import import_snapshot
        from store import _checksum
        source = Path(self.temp.name) / "portable"; source.mkdir()
        store = initialize_store(source, database="sqlite"); store.close()
        rows = []
        for i in range(100):
            document = {"id": f"person-{i:03}", "description": "é漢字 " + "x" * 100_000}
            rows.append((document["id"], json.dumps(document, ensure_ascii=False), 1, _checksum(document)))
        with sqlite3.connect(source / "store.sqlite3") as connection:
            connection.executemany("INSERT INTO principals VALUES (?, ?, ?, ?)", rows)
        import_snapshot(self.runtime.client, {"database": base64.b64encode((source / "store.sqlite3").read_bytes()).decode(),
                                             "repository": '{"repository_id":"portable"}', "phase": "ready"})
        self.runtime.restart()
        backend = DurableObjectsBackend(self.root)
        store = Store(backend)
        self.assertEqual(100, len(store.load_sections(["principals"])["principals"]))
        store.close()
        exported = Path(self.temp.name) / "export.sqlite3"
        backend.dump(exported)
        with sqlite3.connect(exported) as connection:
            self.assertEqual(rows, connection.execute("SELECT key,doc,version,checksum FROM principals ORDER BY key").fetchall())
        self.assertGreater(exported.stat().st_size, 8 * 1024 * 1024)
        with self.assertRaisesRegex(RuntimeError, "existing"):
            import_snapshot(self.runtime.client, {})
        original_call = backend.client.call
        changed = [False]
        def mutate(path, body):
            result = original_call(path, body)
            if path == "/export" and body["table"] == "principals" and not changed[0]:
                changed[0] = True
                self.runtime.client.commit(self.runtime.client.status()["revision"], [{"sql": "DELETE FROM teams", "params": []}])
            return result
        failed = Path(self.temp.name) / "inconsistent.sqlite3"
        with patch.object(backend.client, "call", side_effect=mutate), self.assertRaises(ForgeError):
            backend.dump(failed)
        self.assertFalse(failed.exists())
