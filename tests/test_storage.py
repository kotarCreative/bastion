"""Storage backend matrix: versions, leases, checksums, audit, and stats.

The same core assertions run against SQLite and, when a server is reachable,
against PostgreSQL through ``BASTION_TEST_PG``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from errors import ForgeError
from store import (
    _PostgresBackend,
    _SqliteBackend,
    _checksum,
    initialize_store,
    open_store,
    read_backend_record,
    write_backend_record,
)


PG_URI = os.environ.get("BASTION_TEST_PG", "")


def document() -> dict:
    return {
        "canonical_branch": "main",
        "repository_state": "ready",
        "repository_id": "a" * 32,
        "last_indexed_seq": 0,
        "views": {"alice": {"projected_tip": "0" * 40}},
        "transactions": {},
        "projection_jobs": {},
        "changes": {},
        "principals": {},
        "teams": {},
        "roles": {},
        "role_bindings": {},
        "view_descriptors": {},
    }


class BackendMatrix:
    """Mixin running each assertion against every configured backend."""

    def forges(self) -> list[tuple[str, Path]]:
        cases: list[tuple[str, Path]] = []
        temporary = tempfile.mkdtemp()
        root = Path(temporary) / "sqlite-forge"
        root.mkdir(parents=True)
        initialize_store(root, "sqlite").close()
        cases.append(("sqlite", root))
        if PG_URI:
            pg_root = Path(temporary) / "pg-forge"
            pg_root.mkdir(parents=True)
            initialize_store(pg_root, PG_URI).close()
            cases.append(("postgres", pg_root))
        self.addCleanup(_cleanup_temp, Path(temporary))
        return cases


def _cleanup_temp(directory: Path) -> None:
    import shutil

    shutil.rmtree(directory, ignore_errors=True)


class StorageCoreTest(BackendMatrix, unittest.TestCase):
    def test_round_trip_and_version_bumps(self) -> None:
        for name, root in self.forges():
            with self.subTest(name):
                store = open_store(root)
                doc = document()
                store.save_document(doc, [], None)
                loaded = store.load_document()
                self.assertEqual("0" * 40, loaded["views"]["alice"]["projected_tip"])
                first = _row_version(root, name, "views", "alice")
                doc["views"]["alice"]["projected_tip"] = "1" * 40
                store.save_document(doc, [], None)
                second = _row_version(root, name, "views", "alice")
                self.assertEqual(first + 1, second)
                # Unchanged documents keep their version.
                store.save_document(doc, [], None)
                third = _row_version(root, name, "views", "alice")
                self.assertEqual(second, third)
                store.close()

    def test_delete_propagates(self) -> None:
        for name, root in self.forges():
            with self.subTest(name):
                store = open_store(root)
                doc = document()
                doc["views"]["bob"] = {"present": True}
                store.save_document(doc, [], None)
                del doc["views"]["bob"]
                store.save_document(doc, [], None)
                self.assertEqual(["alice"], sorted(store.load_document()["views"]))
                store.close()

    def test_tamper_detection_names_the_aggregate(self) -> None:
        for name, root in self.forges():
            with self.subTest(name):
                store = open_store(root)
                store.save_document(document(), [], None)
                store.close()
                _corrupt_row(root, name, "views", "alice", "doc")
                with self.assertRaises(ForgeError) as caught:
                    open_store(root).load_document()
                self.assertIn("views.alice", str(caught.exception))

    def test_checksum_column_tamper_detected(self) -> None:
        for name, root in self.forges():
            with self.subTest(name):
                store = open_store(root)
                store.save_document(document(), [], None)
                store.close()
                _corrupt_row(root, name, "transactions", "__none__", None)
                store = open_store(root)
                doc = document()
                doc["transactions"] = {"tx-1": {"status": "prepared"}}
                store.save_document(doc, [], None)
                store.close()
                _corrupt_row(root, name, "transactions", "tx-1", "checksum")
                with self.assertRaises(ForgeError):
                    open_store(root).load_document()

    def test_uninitialized_forge_refuses_to_load(self) -> None:
        for name, root in self.forges():
            with self.subTest(name):
                fresh = Path(tempfile.mkdtemp()) / "fresh"
                fresh.mkdir(parents=True)
                self.addCleanup(_cleanup_temp, fresh.parent)
                initialize_store(fresh, PG_URI if name == "postgres" else None).close()
                with self.assertRaises(ForgeError):
                    open_store(fresh).load_document()

    def test_wrong_backend_record_is_fatal(self) -> None:
        root = Path(tempfile.mkdtemp()) / "forge"
        root.mkdir(parents=True)
        self.addCleanup(_cleanup_temp, root.parent)
        initialize_store(root).close()
        target = root / "backend.json"
        target.write_text('{"backend": "oracle"}')
        with self.assertRaises(ForgeError):
            open_store(root)

    def test_missing_backend_record_is_fatal(self) -> None:
        root = Path(tempfile.mkdtemp()) / "forge"
        root.mkdir(parents=True)
        self.addCleanup(_cleanup_temp, root.parent)
        with self.assertRaises(ForgeError):
            open_store(root)


class LeaseTest(BackendMatrix, unittest.TestCase):
    def test_mutual_exclusion_within_scope(self) -> None:
        for name, root in self.forges():
            with self.subTest(name):
                store = open_store(root)
                owner_a = f"a:{os.getpid()}:{_fake_start()}"
                owner_b = f"b:{os.getpid()}:{_fake_start()}"
                store.acquire_leases(["global"], owner_a)
                try:
                    with self.assertRaises(ForgeError):
                        store.acquire_leases(["global"], owner_b)
                finally:
                    store.release_leases(["global"], owner_a)
                # Released: the other owner proceeds.
                store.acquire_leases(["global"], owner_b)
                store.release_leases(["global"], owner_b)
                store.close()

    def test_scopes_are_independent(self) -> None:
        store = next(iter(open_store(root) for _, root in [self.forges()[0]]))
        try:
            root = self.forges()[0][1]
            store.close()
            store = open_store(root)
            store.acquire_leases(["view:a"], f"a:{os.getpid()}:{_fake_start()}")
            store.acquire_leases(["view:b"], f"b:{os.getpid()}:{_fake_start()}")
            store.release_leases(["view:a"], f"a:{os.getpid()}:{_fake_start()}")
            store.release_leases(["view:b"], f"b:{os.getpid()}:{_fake_start()}")
        finally:
            store.close()

    def test_expired_lease_is_stealable(self) -> None:
        for name, root in self.forges():
            with self.subTest(name):
                from store import LEASE_TTL_SECONDS

                store = open_store(root)
                ghost = f"ghost:{os.getpid()}:{_fake_start()}"
                store.acquire_leases(["global"], ghost)
                _expire_leases(root, name, by=LEASE_TTL_SECONDS + 5)
                successor = f"next:{os.getpid()}:{_fake_start()}"
                store.acquire_leases(["global"], successor)
                store.release_leases(["global"], successor)
                store.close()

    def test_dead_owner_process_lease_is_stolen_immediately(self) -> None:
        for name, root in self.forges():
            with self.subTest(name):
                store = open_store(root)
                dead_owner = f"dead:999999:1234567890"
                store.acquire_leases(["global"], dead_owner)
                successor = f"live:{os.getpid()}:{_fake_start()}"
                started = time.time()
                store.acquire_leases(["global"], successor)
                self.assertLess(time.time() - started, 5.0)
                store.release_leases(["global"], successor)
                store.close()

    def test_save_with_lost_lease_aborts(self) -> None:
        for name, root in self.forges():
            with self.subTest(name):
                store = open_store(root)
                owner = f"a:{os.getpid()}:{_fake_start()}"
                store.acquire_leases(["global"], owner)
                thief = f"thief:{os.getpid()}:{_fake_start()}"
                _steal_global_lease(root, name, thief)
                with self.assertRaises(ForgeError):
                    store.save_document(document(), [("global", owner)], None)
                store.close()


class AuditTest(BackendMatrix, unittest.TestCase):
    def test_audit_appends_and_lists(self) -> None:
        for name, root in self.forges():
            with self.subTest(name):
                store = open_store(root)
                store.audit("job.dead_letter", "job-1", "first")
                store.audit("job.cancelled", "job-2", "second")
                entries = store.recent_audit(10)
                self.assertEqual(2, len(entries))
                self.assertEqual("second", entries[0]["detail"])
                store.close()

    def test_stats_shape(self) -> None:
        for name, root in self.forges():
            with self.subTest(name):
                store = open_store(root)
                doc = document()
                doc["transactions"] = {"t": {"status": "published"}}
                doc["projection_jobs"] = {
                    "j1": {"status": "completed"},
                    "j2": {"status": "dead"},
                }
                store.save_document(doc, [], None)
                stats = store.stats()
                self.assertEqual(name, stats["backend"])
                self.assertEqual({"published": 1}, stats["counts"]["transactions"])
                self.assertEqual({"completed": 1, "dead": 1}, stats["counts"]["jobs"])
                self.assertEqual(1, stats["dead_letters"])
                self.assertGreaterEqual(stats["size_bytes"], 0)
                store.close()


class HostileIdentifierTest(BackendMatrix, unittest.TestCase):
    def test_hostile_keys_round_trip(self) -> None:
        hostile = [
            'robert"); DROP TABLE views;--',
            "line\nbreak",
            "tab\tstop",
            "unicode-é☃",
            "'single'quotes",
            '"double"quotes',
            "back\\slash",
            "%s?%s%s",
            "x" * 512,
        ]
        for name, root in self.forges():
            with self.subTest(name):
                store = open_store(root)
                doc = document()
                doc["views"] = {key: {"marker": index} for index, key in enumerate(hostile)}
                store.save_document(doc, [], None)
                reloaded = store.load_document()["views"]
                for key in hostile:
                    self.assertIn(key, reloaded)
                store.close()


def _row_version(root: Path, backend: str, table: str, key: str) -> int:
    if backend == "sqlite":
        connection = sqlite3.connect(root / "store.sqlite3")
        try:
            return connection.execute(
                f"SELECT version FROM {table} WHERE key = ?", (key,)
            ).fetchone()[0]
        finally:
            connection.close()
    import psycopg

    dsn = _pg_dsn_for(root)
    with psycopg.connect(dsn) as connection:
        return connection.execute(
            f"SELECT version FROM {table} WHERE key = %s", (key,)
        ).fetchone()[0]


def _pg_dsn_for(root: Path) -> str:
    base = PG_URI.rstrip("/")
    digest = __import__("hashlib").sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return f"{base}/bastion_{digest}"


def _corrupt_row(
    root: Path, backend: str, table: str, key: str, column: str | None
) -> None:
    if column is None:
        return
    if backend == "sqlite":
        connection = sqlite3.connect(root / "store.sqlite3")
        try:
            if column == "doc":
                connection.execute(
                    f"UPDATE {table} SET doc = '{{\"tampered\": true}}' WHERE key = ?",
                    (key,),
                )
            else:
                connection.execute(
                    f"UPDATE {table} SET checksum = 'deadbeef' WHERE key = ?", (key,)
                )
            connection.commit()
        finally:
            connection.close()
        return
    import psycopg

    expression = (
        "doc = '{\"tampered\": true}'" if column == "doc" else "checksum = 'deadbeef'"
    )
    with psycopg.connect(_pg_dsn_for(root), autocommit=True) as connection:
        connection.execute(f"UPDATE {table} SET {expression} WHERE key = %s", (key,))


def _expire_leases(root: Path, backend: str, by: float) -> None:
    if backend == "sqlite":
        connection = sqlite3.connect(root / "store.sqlite3")
        try:
            connection.execute(
                "UPDATE storage_locks SET expires_at = expires_at - ?", (by,)
            )
            connection.commit()
        finally:
            connection.close()
        return
    import psycopg

    with psycopg.connect(_pg_dsn_for(root), autocommit=True) as connection:
        connection.execute(
            "UPDATE storage_locks SET expires_at = expires_at - %s", (by,)
        )


def _steal_global_lease(root: Path, backend: str, owner: str) -> None:
    statement = (
        "UPDATE storage_locks SET owner = ? WHERE scope = 'global'"
        if backend == "sqlite"
        else "UPDATE storage_locks SET owner = %s WHERE scope = 'global'"
    )
    if backend == "sqlite":
        connection = sqlite3.connect(root / "store.sqlite3")
        try:
            connection.execute(statement, (owner,))
            connection.commit()
        finally:
            connection.close()
        return
    import psycopg

    with psycopg.connect(_pg_dsn_for(root), autocommit=True) as connection:
        connection.execute(statement, (owner,))


def _fake_start() -> str:
    fields = Path(f"/proc/{os.getpid()}/stat").read_text().split()
    return fields[21]


if __name__ == "__main__":
    unittest.main()
