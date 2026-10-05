from __future__ import annotations

import json
import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from cloudflare_runtime import (
    CONTROL_KEY,
    DurableRuntime,
    _read_snapshot,
    checkpoint_root,
)
from object_store import Create, StorageFailure
from object_store_memory import MemoryObjectStore


def make_root(path: Path, value: str = "initial") -> None:
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.closing(sqlite3.connect(path / "store.sqlite3")) as database:
        database.execute("CREATE TABLE catalog (value TEXT NOT NULL)")
        database.execute("INSERT INTO catalog VALUES (?)", (value,))
        database.commit()
    (path / "backend.json").write_text('{"backend":"sqlite"}\n')
    (path / "repository.json").write_text('{"repository_id":"test"}\n')


class FailingStore(MemoryObjectStore):
    fail_put = False

    def put(self, key, body, *, mode):
        if self.fail_put:
            raise StorageFailure("remote unavailable")
        return super().put(key, body, mode=mode)


class CloudflareRuntimeTest(unittest.TestCase):
    def test_snapshot_is_atomic_checksummed_and_restores_sqlite_config(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            make_root(root)
            store = MemoryObjectStore()
            version = checkpoint_root(root, store)
            payload, restored_version = _read_snapshot(store)
            self.assertEqual(version, restored_version)
            self.assertEqual("sqlite", json.loads(payload["backend"])["backend"])

            with tempfile.TemporaryDirectory() as restored_dir:
                restored = Path(restored_dir) / "root"
                from cloudflare_runtime import restore_root

                restore_root(restored, payload)
                with contextlib.closing(sqlite3.connect(restored / "store.sqlite3")) as database:
                    self.assertEqual("initial", database.execute("SELECT value FROM catalog").fetchone()[0])
                self.assertEqual((root / "repository.json").read_bytes(), (restored / "repository.json").read_bytes())

            saved = store.get(CONTROL_KEY).body
            envelope = json.loads(saved)
            envelope["payload"]["backend"] = "{}"
            store.put(CONTROL_KEY, json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode(), mode=__import__("object_store").Update(version))
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                _read_snapshot(store)

    def test_checkpoint_rejects_postgres_before_opening_a_sqlite_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            root.mkdir()
            (root / "backend.json").write_text('{"backend":"postgres","dsn":"postgres://hidden"}')
            (root / "repository.json").write_text('{"repository_id":"test"}')
            with self.assertRaisesRegex(RuntimeError, "require SQLite"):
                checkpoint_root(root, MemoryObjectStore())
            self.assertFalse((root / "store.sqlite3").exists())

    def test_mutation_checkpoints_before_success_and_survives_new_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            make_root(root)
            store = MemoryObjectStore()
            checkpoint_root(root, store)
            checkpoints = []

            def app(environ, start_response):
                with contextlib.closing(sqlite3.connect(root / "store.sqlite3")) as database:
                    database.execute("UPDATE catalog SET value='committed'")
                    database.commit()
                from state import _notify_state_commit

                _notify_state_commit(root)
                checkpoints.append(store.get(CONTROL_KEY).meta.version.value)
                start_response("200 OK", [("Content-Length", "2")])
                return [b"ok"]

            runtime = DurableRuntime(app, root, store)
            output = []
            result = runtime(
                {"REQUEST_METHOD": "POST", "PATH_INFO": "/api/v1/policy"},
                lambda status, headers: output.append(status),
            )
            self.assertEqual([b"ok"], list(result))
            self.assertEqual(["200 OK"], output)
            self.assertEqual(1, len(checkpoints))

            import shutil

            shutil.rmtree(root)
            restarted = DurableRuntime(app, root, store)
            with contextlib.closing(sqlite3.connect(root / "store.sqlite3")) as database:
                self.assertEqual("committed", database.execute("SELECT value FROM catalog").fetchone()[0])
            self.assertFalse(restarted.poisoned)

    def test_failed_publish_returns_503_and_poisoned_runtime_stays_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "root"
            make_root(root)
            store = FailingStore()
            checkpoint_root(root, store)

            def app(_environ, start_response):
                start_response("200 OK", [("Content-Length", "2")])
                return [b"ok"]

            runtime = DurableRuntime(app, root, store)
            store.fail_put = True
            for _ in range(2):
                output = []
                body = b"".join(runtime(
                    {"REQUEST_METHOD": "POST", "PATH_INFO": "/api/v1/policy"},
                    lambda status, headers: output.append(status),
                ))
                self.assertEqual("503 Service Unavailable", output[0])
                self.assertEqual(b"durable storage unavailable", body)
            self.assertTrue(runtime.poisoned)


if __name__ == "__main__":
    unittest.main()
