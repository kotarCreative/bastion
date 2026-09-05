"""Spec 014 storage correctness: leases, lifecycle, backup/restore, workspaces.

Covers the PostgreSQL-lifecycle/lease/backup/restore/receive/snapshot and
targeted-access verification bullets that run without a live PostgreSQL
server. PostgreSQL-gated cases skip unless ``BASTION_TEST_PG`` is set.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import store as store_module
from errors import ForgeError
from store import (
    _PostgresBackend,
    host_forge_id,
    initialize_store,
    open_store,
)


def _cleanup(directory: Path) -> None:
    import shutil

    shutil.rmtree(directory, ignore_errors=True)


def _make_sqlite_root(test: unittest.TestCase) -> Path:
    base = Path(tempfile.mkdtemp())
    test.addCleanup(_cleanup, base)
    root = base / "forge"
    root.mkdir(parents=True)
    initialize_store(root, "sqlite").close()
    return root


def _pid_start() -> tuple[int, str]:
    pid = os.getpid()
    fields = Path(f"/proc/{pid}/stat").read_text().split()
    return pid, fields[21]


def _legacy_owner(token: str) -> str:
    pid, start = _pid_start()
    return f"{token}:{pid}:{start}"


class LeaseRaceTest(unittest.TestCase):
    def test_barrier_contenders_elect_exactly_one_live_owner(self) -> None:
        root = _make_sqlite_root(self)
        barrier = threading.Barrier(2)
        outcomes: dict[str, str] = {}

        def contender(name: str) -> None:
            store = open_store(root)
            try:
                barrier.wait(timeout=10)
                store.acquire_leases(["race-scope"], _legacy_owner(name), root)
                outcomes[name] = "won"
            except ForgeError:
                outcomes[name] = "lost"
            except Exception as error:  # pragma: no cover - test harness
                outcomes[name] = f"error: {error}"
            finally:
                store.close()

        with patch.object(store_module, "LEASE_WAIT_SECONDS", 3.0):
            threads = [
                threading.Thread(target=contender, args=(name,))
                for name in ("a", "b")
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
        self.assertEqual(sorted(outcomes.values()), ["lost", "won"])

    def test_expired_and_dead_leases_are_recoverable(self) -> None:
        root = _make_sqlite_root(self)
        store = open_store(root)
        try:
            store.acquire_leases(["scope"], "ghost:999999:1", root)
            store.acquire_leases(["dead"], "dead:999999:1234567890", root)
            # Both holders are dead PIDs: immediate same-host recovery.
            started = time.time()
            store.acquire_leases(["dead"], _legacy_owner("live"), root)
            self.assertLess(time.time() - started, 5.0)
            store.release_leases(["dead"], _legacy_owner("live"))
            # Expire the ghost lease, then steal by expiry.
            connection = sqlite3.connect(root / "store.sqlite3")
            try:
                connection.execute(
                    "UPDATE storage_locks SET expires_at = expires_at - 10000"
                    " WHERE scope = 'scope'"
                )
                connection.commit()
            finally:
                connection.close()
            store.acquire_leases(["scope"], _legacy_owner("next"), root)
            store.release_leases(["scope"], _legacy_owner("next"))
        finally:
            store.close()

    def test_multi_scope_contention_rolls_back_without_partial_leases(self) -> None:
        root = _make_sqlite_root(self)
        first = open_store(root)
        second = open_store(root)
        try:
            first.acquire_leases(["held"], _legacy_owner("owner"), root)
            with patch.object(store_module, "LEASE_WAIT_SECONDS", 1.0):
                with self.assertRaises(ForgeError):
                    second.acquire_leases(["fresh", "held"], _legacy_owner("rival"), root)
            rows = first._fetchall(
                "SELECT scope, owner FROM storage_locks WHERE scope = ?", ("fresh",)
            )
            self.assertEqual([], rows)
            held = first._fetchall(
                "SELECT owner FROM storage_locks WHERE scope = ?", ("held",)
            )
            self.assertTrue(held[0]["owner"].startswith("owner:"))
        finally:
            first.close()
            second.close()

    def test_foreign_host_owner_is_not_stolen_before_expiry(self) -> None:
        from store import _linux_boot_id

        root = _make_sqlite_root(self)
        local_host = host_forge_id(root)
        self.assertIsNotNone(local_host)
        pid, start = _pid_start()
        boot = _linux_boot_id() or "unknown-boot"
        foreign = f"tok:ffffffffffffffffffffffffffffffff:{boot}:{pid}:{start}"
        store = open_store(root)
        try:
            store.acquire_leases(["foreign"], foreign, root)
            with patch.object(store_module, "LEASE_WAIT_SECONDS", 1.0):
                with self.assertRaises(ForgeError):
                    store.acquire_leases(["foreign"], _legacy_owner("local"), root)
            # Expiry-based foreign recovery still works.
            connection = sqlite3.connect(root / "store.sqlite3")
            try:
                connection.execute(
                    "UPDATE storage_locks SET expires_at = expires_at - 10000"
                    " WHERE scope = 'foreign'"
                )
                connection.commit()
            finally:
                connection.close()
            store.acquire_leases(["foreign"], _legacy_owner("local"), root)
            store.release_leases(["foreign"], _legacy_owner("local"))
        finally:
            store.close()

    def test_heartbeat_is_committed_and_exact_scope_loss_rolls_back(self) -> None:
        from state import heartbeat_session

        root = _make_sqlite_root(self)
        store = open_store(root)
        try:
            owner = f"{'t' * 32}:{_legacy_owner('x').split(':', 1)[1]}"
            first_expiry = store.acquire_leases(["h1", "h2"], owner, root)
            self.assertGreater(first_expiry, time.time())
        finally:
            store.close()
        returned = heartbeat_session(root, owner, 120.0, ["h1", "h2"])
        # A second connection observes the persisted expiry after close.
        observer = open_store(root)
        try:
            rows = {
                row["scope"]: row["expires_at"]
                for row in observer._fetchall(
                    "SELECT scope, expires_at FROM storage_locks", ()
                )
            }
        finally:
            observer.close()
        self.assertAlmostEqual(rows["h1"], returned, delta=5.0)
        self.assertAlmostEqual(rows["h2"], returned, delta=5.0)
        # Lose one scope, then heartbeat the exact set: rollback, lease loss,
        # and the surviving scope keeps its committed expiry.
        thief = open_store(root)
        try:
            with patch.object(store_module, "LEASE_WAIT_SECONDS", 1.0):
                thief.acquire_leases(["h2"], _legacy_owner("thief"), root)
        except ForgeError:
            connection = sqlite3.connect(root / "store.sqlite3")
            try:
                connection.execute(
                    "UPDATE storage_locks SET owner = ?, expires_at = ?"
                    " WHERE scope = 'h2'",
                    (_legacy_owner("thief"), time.time() + 600),
                )
                connection.commit()
            finally:
                connection.close()
        finally:
            thief.close()
        before = rows["h1"]
        with self.assertRaisesRegex(ForgeError, "lease lost"):
            heartbeat_session(root, owner, 120.0, ["h1", "h2"])
        observer = open_store(root)
        try:
            after = observer._fetchone(
                "SELECT expires_at FROM storage_locks WHERE scope = ?", ("h1",)
            )
        finally:
            observer.close()
        self.assertAlmostEqual(after["expires_at"], before, delta=0.5)


class LifecycleTest(unittest.TestCase):
    def test_missing_sqlite_database_is_one_stable_error(self) -> None:
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        root = base / "forge"
        root.mkdir(parents=True)
        initialize_store(root, "sqlite").close()
        (root / "store.sqlite3").unlink()
        with self.assertRaisesRegex(ForgeError, "not initialized"):
            open_store(root)
        self.assertFalse((root / "store.sqlite3").exists())

    def test_ordinary_postgres_open_never_touches_the_admin_database(self) -> None:
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        root = base / "forge"
        root.mkdir(parents=True)
        runtime = "postgresql://runtime:secret@db.internal:5432/app"
        (root / "backend.json").write_text(json.dumps({"backend": "postgres", "dsn": runtime}))
        seen: list[str] = []
        real_connect = None
        try:
            import psycopg  # noqa: F401

            import psycopg as _psycopg

            real_connect = _psycopg.connect
        except ImportError:
            self.skipTest("psycopg is not installed")

        def fake_connect(dsn: str, **kwargs):
            seen.append(dsn)
            raise RuntimeError("no server in this test")

        with patch.object(_psycopg, "connect", side_effect=fake_connect):
            with self.assertRaisesRegex(ForgeError, "unavailable") as caught:
                open_store(root).close()
        self.assertTrue(seen)
        for dsn in seen:
            self.assertNotIn("/postgres", dsn)
        # The stable storage error never carries credentials or the DSN.
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("db.internal", str(caught.exception))

    def test_owner_identity_file_is_private_and_excluded_from_backups(self) -> None:
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        host_id = host_forge_id(fixture.root)
        self.assertIsNotNone(host_id)
        mode = (fixture.root / ".host-id").stat().st_mode & 0o777
        self.assertEqual(0o600, mode)
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        from store import backup_forge

        backup_forge(fixture.root, base / "backup")
        names = {path.name for path in (base / "backup").rglob("*")}
        self.assertNotIn(".host-id", names)


class BackupSafetyTest(unittest.TestCase):
    def test_manifest_inventories_every_artifact_without_credentials(self) -> None:
        from store import backup_forge
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        backup_forge(fixture.root, base / "backup")
        metadata = json.loads((base / "backup" / "manifest.json").read_text())
        self.assertEqual("sqlite", metadata["backend"])
        self.assertIn("last_indexed_seq", metadata)
        self.assertIn("wal_head", metadata)
        entries = metadata["artifacts"]
        by_path = {entry["path"]: entry for entry in entries}
        for entry in entries:
            self.assertEqual(
                {"category", "path", "size", "sha256"}, set(entry),
                f"artifact entry shape: {entry}",
            )
            self.assertIn(entry["category"], {"object", "config", "database"})
            body = (base / "backup" / entry["path"]).read_bytes()
            self.assertEqual(len(body), entry["size"])
            import hashlib

            self.assertEqual(hashlib.sha256(body).hexdigest(), entry["sha256"])
        self.assertIn("repository.json", by_path)
        self.assertIn("backend-kind.json", by_path)
        self.assertIn("database.sqlite3", by_path)
        # The source backend record (and any DSN) is never copied.
        self.assertNotIn("backend.json", by_path)
        self.assertFalse((base / "backup" / "backend.json").exists())
        kind = json.loads((base / "backup" / "backend-kind.json").read_text())
        self.assertEqual({"backend": "sqlite"}, kind)

    def test_restore_preserves_audit_sequence_and_drops_leases(self) -> None:
        from store import backup_forge, restore_forge
        from tests.storage_helpers import repository_state
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        store = open_store(fixture.root)
        try:
            store.audit("probe.event", "subject", "detail")
            before = store.recent_audit(1000)
        finally:
            store.close()
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        backup_forge(fixture.root, base / "backup")
        # Live leases exist at backup time but must not be restored.
        live = open_store(fixture.root)
        try:
            live.acquire_leases(["backup-probe"], _legacy_owner("live"), fixture.root)
            backup_forge(fixture.root, base / "backup2")
        finally:
            try:
                live.release_leases(["backup-probe"], _legacy_owner("live"))
            except ForgeError:
                pass
            live.close()
        restored = base / "restored"
        restore_forge(base / "backup", restored)
        store = open_store(restored)
        try:
            after = store.recent_audit(1000)
            self.assertEqual(
                [(entry["seq"], entry["kind"], entry["event_id"]) for entry in before],
                [(entry["seq"], entry["kind"], entry["event_id"]) for entry in after],
            )
            leases = store._fetchall("SELECT scope FROM storage_locks", ())
            self.assertEqual([], leases)
            # The audit sequence generator continues past restored rows.
            peak = max(entry["seq"] for entry in after)
            store.audit("probe.after", "subject", "detail")
            self.assertEqual(peak + 1, store.recent_audit(1)[0]["seq"])
        finally:
            store.close()
        original_manifest, _ = repository_state(fixture.root)
        restored_manifest, _ = repository_state(restored)
        self.assertEqual(original_manifest.to_dict(), restored_manifest.to_dict())

    def test_sqlite_restore_rejects_bad_artifacts_and_cleans_the_target(self) -> None:
        import shutil
        from store import backup_forge, restore_forge
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        backup_forge(fixture.root, base / "good")

        def attempt(mutator, target_name: str) -> None:
            candidate = base / f"bad-{target_name}"
            if candidate.exists():
                shutil.rmtree(candidate)
            shutil.copytree(base / "good", candidate)
            mutator(candidate)
            # Rewrite the manifest artifact hash when the mutator changed a
            # file without updating inventory, to isolate each failure mode.
            with self.assertRaises(ForgeError):
                restore_forge(candidate, base / f"target-{target_name}")
            self.assertFalse((base / f"target-{target_name}").exists())

        def corrupt_db(path: Path) -> None:
            with (path / "database.sqlite3").open("r+b") as handle:
                handle.seek(100)
                handle.write(b"\x00\xff\x00\xff")

        attempt(corrupt_db, "corrupt")
        attempt(lambda path: (path / "database.sqlite3").unlink(), "missing")

        def oversized(path: Path) -> None:
            with (path / "database.sqlite3").open("ab") as handle:
                handle.write(b"\x00")

        attempt(oversized, "oversized")
        # Cross-backend restore fails before target mutation.
        with self.assertRaisesRegex(ForgeError, "same backend kind"):
            restore_forge(base / "good", base / "pg-target", "postgresql://x/y")
        self.assertFalse((base / "pg-target").exists())

    def test_table_allowlist_rejects_hostile_filenames_without_sql(self) -> None:
        from store import _read_restore_tables
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        tables = base / "tables"
        tables.mkdir()
        hostile = [
            'views"; DROP TABLE views;--.ndjson',
            "views'; DELETE FROM principals;--.ndjson",
            "views/*.ndjson",
            "..ndjson",
            "unicode-é.ndjson",
            " views .ndjson",
            "audit_log.ndjson ",
        ]
        for name in hostile:
            with self.assertRaises(ForgeError, msg=name):
                target = tables / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("{}\n")
                try:
                    _read_restore_tables(tables)
                finally:
                    if target.is_file() or target.is_symlink():
                        target.unlink(missing_ok=True)
        # The live forge is untouched by every rejected attempt.
        store = open_store(fixture.root)
        try:
            self.assertTrue(store.load_document()["views"])
        finally:
            store.close()

    def test_table_rows_are_shape_checked_before_mutation(self) -> None:
        import shutil
        from store import _read_restore_tables

        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        tables = base / "tables"
        from store import _RESTORE_TABLE_ALLOWLIST

        import hashlib as _hashlib

        def meta_row() -> str:
            doc = {
                "canonical_branch": "main",
                "repository_state": "ready",
                "repository_id": "a" * 32,
                "last_indexed_seq": 0,
            }
            return json.dumps(
                {
                    "key": 1,
                    "doc": doc,
                    "version": 1,
                    "checksum": _hashlib.sha256(
                        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
                    ).hexdigest(),
                }
            )

        def audit_row() -> str:
            return json.dumps(
                {
                    "seq": 1,
                    "kind": "k",
                    "subject": "s",
                    "detail": "d",
                    "at": 1.0,
                    "event_id": None,
                }
            )

        def write_valid(mutate_table: str | None = None, mutate=None) -> None:
            shutil.rmtree(tables, ignore_errors=True)
            tables.mkdir()
            for table in _RESTORE_TABLE_ALLOWLIST:
                rows: list[str] = []
                if table == "forge_meta":
                    rows.append(meta_row())
                elif table == "audit_log":
                    rows.append(audit_row())
                if table == mutate_table and mutate is not None:
                    rows = mutate(rows)
                (tables / f"{table}.ndjson").write_text("\n".join(rows) + "\n")

        def break_checksum(rows: list[str]) -> list[str]:
            row = json.loads(rows[0])
            row["checksum"] = "0" * 64
            return [json.dumps(row)]

        write_valid("forge_meta", break_checksum)
        with self.assertRaisesRegex(ForgeError, "checksum"):
            _read_restore_tables(tables)

        write_valid()
        parsed = _read_restore_tables(tables)
        self.assertEqual(1, len(parsed["forge_meta"]))

        write_valid("forge_meta", lambda rows: rows + rows)
        with self.assertRaisesRegex(ForgeError, "duplicated|invalid|once"):
            _read_restore_tables(tables)

        def trailing_field(rows: list[str]) -> list[str]:
            row = json.loads(rows[0])
            row["extra"] = 1
            return [json.dumps(row)]

        write_valid("forge_meta", trailing_field)
        with self.assertRaisesRegex(ForgeError, "shape"):
            _read_restore_tables(tables)

        def bad_line(rows: list[str]) -> list[str]:
            return ["not json{"]

        write_valid("audit_log", bad_line)
        with self.assertRaisesRegex(ForgeError, "malformed"):
            _read_restore_tables(tables)

        # Unknown table artifacts are never tolerated.
        write_valid()
        (tables / "storage_locks.ndjson").write_text("{}\n")
        with self.assertRaisesRegex(ForgeError, "unexpected"):
            _read_restore_tables(tables)

class ReceiveIsolationTest(unittest.TestCase):
    def test_concurrent_identical_receives_use_private_workspaces_and_converge(self) -> None:
        import protocol
        import protocol as protocol_module
        from materialization import (
            ensure_canonical_materialized,
            ensure_view_materialized,
        )
        from protocol import (
            cleanup_transaction_workspace,
            new_transaction_workspace,
            prepare_submission,
        )
        from repository import Repository
        from state import load_state_sections
        from tests.storage_helpers import repository_state
        from tests.tree_harness import TreeFixture, build_commit, command, oid

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        client = fixture.clients["alice"]
        old = oid(client, "refs/heads/main")
        new = build_commit(
            client,
            old,
            {b"shared/concurrent.txt": (b"100644", b"concurrent\n")},
            b"concurrent\n",
            1_700_000_500,
        )
        # Stage the submitted objects into the intake store without receiving.
        intake = fixture.root / "intake" / "alice.git"
        command("git", "-C", intake, "fetch", "-q", str(client), new)
        repository = Repository.open(fixture.root)
        manifest, replay = repository_state(fixture.root)
        active = replay.active_views["alice"]
        source = ensure_view_materialized(
            fixture.root, "alice", manifest, replay, repository
        )
        view = {
            "principal_id": active["principal_id"],
            "descriptor_id": active["descriptor_id"],
            "descriptor_generation": active["descriptor_generation"],
            "projection_epoch": active["projection_epoch"],
            "canonical_ref": manifest.canonical_ref,
            "projected_ref": active["projected_ref"],
            "canonical_frontier": source.cursor["canonical_frontier"],
            "projected_tip": source.projected_tip,
        }
        descriptor = repository._descriptor(active["descriptor_id"])
        canonical = ensure_canonical_materialized(fixture.root, manifest, repository)
        hook_env = dict(os.environ)
        received_at = int(time.time())
        control = load_state_sections(fixture.root, ["principals"])
        barrier = threading.Barrier(2)
        real_reconcile = protocol_module.reconcile_commits

        def gated(*args, **kwargs):
            barrier.wait(timeout=60)
            return real_reconcile(*args, **kwargs)

        created: list[Path] = []
        real_workspace = protocol_module.new_transaction_workspace

        def recording(root: Path, name: str, old_oid: str, new_oid: str) -> Path:
            path = real_workspace(root, name, old_oid, new_oid)
            created.append(path)
            return path

        results: dict[int, object] = {}
        barrier_hook = threading.Barrier(2)

        def run(index: int) -> None:
            os.environ["BASTION_VIEW"] = "alice"
            os.environ["BASTION_TRANSPORT"] = "local"
            barrier_hook.wait(timeout=60)
            try:
                transition = prepare_submission(
                    fixture.root,
                    "alice",
                    view,
                    descriptor,
                    old,
                    new,
                    hook_env,
                    state=control,
                    canonical=canonical,
                    received_at=received_at,
                )
                results[index] = transition.candidate_object_dir
            except Exception as error:  # pragma: no cover - diagnostics
                results[index] = error

        with patch.object(
            protocol_module, "reconcile_commits", side_effect=gated
        ), patch.object(
            protocol_module, "new_transaction_workspace", side_effect=recording
        ):
            threads = [threading.Thread(target=run, args=(i,)) for i in (0, 1)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)
        try:
            dirs = [results[0], results[1]]
            self.assertTrue(all(isinstance(item, Path) for item in dirs), results)
            self.assertNotEqual(dirs[0], dirs[1])
            self.assertTrue(dirs[0].is_dir() and dirs[1].is_dir())
            # A losing attempt cannot remove a peer's objects.
            cleanup_transaction_workspace(dirs[0])
            self.assertFalse(dirs[0].exists())
            self.assertTrue(dirs[1].is_dir())
            cleanup_transaction_workspace(dirs[1])
        finally:
            os.environ.pop("BASTION_VIEW", None)
            os.environ.pop("BASTION_TRANSPORT", None)
        self.assertEqual(2, len(created))
        self.assertNotEqual(created[0], created[1])
        leftovers = [
            item
            for item in (fixture.root / "transaction-objects").iterdir()
            if item.name.startswith("txn-")
        ]
        self.assertEqual([], leftovers)

    def test_concurrent_identical_pushes_commit_exactly_one_event(self) -> None:
        from protocol import pre_receive
        from tests.storage_helpers import repository_state
        from tests.tree_harness import TreeFixture, build_commit, command, oid

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        client = fixture.clients["alice"]
        old = oid(client, "refs/heads/main")
        new = build_commit(
            client,
            old,
            {b"shared/e2e.txt": (b"100644", b"e2e\n")},
            b"e2e\n",
            1_700_000_600,
        )
        intake = fixture.root / "intake" / "alice.git"
        command("git", "-C", intake, "fetch", "-q", str(client), new)
        _manifest, replay = repository_state(fixture.root)
        principal = replay.active_views["alice"]["principal_id"]
        before = _manifest.head_seq
        updates = f"{old} {new} refs/heads/main".encode()
        errors: list[Exception] = []

        saved_env = dict(os.environ)
        os.environ["BASTION_TRANSPORT"] = "local"
        os.environ["BASTION_PRINCIPAL_ID"] = principal
        os.environ.pop("BASTION_VIEW", None)
        os.environ.pop("BASTION_IDEMPOTENCY_KEY", None)
        try:

            def run() -> None:
                try:
                    pre_receive(fixture.root, "alice", updates)
                except Exception as error:  # pragma: no cover - diagnostics
                    errors.append(error)

            threads = [threading.Thread(target=run) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=180)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)
        self.assertEqual([], errors)
        after, _ = repository_state(fixture.root)
        self.assertEqual(before + 1, after.head_seq)

    def test_stale_cleanup_removes_only_dead_workspaces(self) -> None:
        from protocol import (
            cleanup_stale_transaction_workspaces,
            cleanup_transaction_workspace,
            new_transaction_workspace,
            transaction_parent,
        )

        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        root = base / "forge"
        root.mkdir()
        live = new_transaction_workspace(root, "v", "0" * 40, "1" * 40)
        dead = new_transaction_workspace(root, "v", "0" * 40, "1" * 40)
        import json as _json

        (dead / "owner.json").write_text(
            _json.dumps({"owner": "dead:999999:1", "at": time.time()})
        )
        # The shared parent is never blindly cleared: the live workspace and
        # the transaction parent itself survive.
        removed = cleanup_stale_transaction_workspaces(root, max_age_seconds=3600.0)
        self.assertEqual(1, removed)
        self.assertTrue(live.is_dir())
        self.assertFalse(dead.exists())
        self.assertTrue(transaction_parent(root).is_dir())
        cleanup_transaction_workspace(live)

    def test_invalid_restore_limits_fail_before_restore(self) -> None:
        from store import _read_restore_tables

        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        with patch.dict(os.environ, {"BASTION_RESTORE_MAX_ROWS_PER_TABLE": "many"}):
            with self.assertRaisesRegex(ForgeError, "configuration"):
                _read_restore_tables(base)
        with patch.dict(os.environ, {"BASTION_RESTORE_MAX_ARTIFACT_BYTES": "-5"}):
            with self.assertRaisesRegex(ForgeError, "configuration"):
                _read_restore_tables(base)


class RepositorySnapshotTest(unittest.TestCase):
    def _light_forge(self) -> tuple[TreeFixture, object]:
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        from repository import Repository

        return fixture, Repository.open(fixture.root)

    def test_unchanged_loads_probe_without_body_wal_or_descriptor_reads(self) -> None:
        import repository as repository_module
        from object_store_fs import FilesystemObjectStore

        fixture, repository = self._light_forge()
        repository_module.clear_repository_cache()
        repository_module.reset_repository_load_counts()
        manifest, _version, _replay = repository.load()
        self.assertEqual(1, repository_module.repository_load_counts()["full_loads"])
        real_read = FilesystemObjectStore._read
        body_read_keys: list[str] = []

        def counting(self, key: str):
            body_read_keys.append(key)
            return real_read(self, key)

        fetches_before = repository_module.repository_load_counts()["descriptor_fetches"]
        with patch.object(FilesystemObjectStore, "_read", counting):
            for _ in range(100):
                probe = repository_module.Repository.open(fixture.root).load()
                self.assertEqual(manifest.to_dict(), probe[0].to_dict())
        counts = repository_module.repository_load_counts()
        self.assertEqual(1, counts["full_loads"])
        self.assertEqual(100, counts["conditional_hits"])
        # Fresh handles share the process-global version cache: 100
        # conditional manifest metadata probes, zero manifest body
        # reads/hashes/parses, zero WAL reads, zero descriptor reads.
        self.assertEqual([], body_read_keys)
        self.assertEqual(fetches_before, counts["descriptor_fetches"])

    def test_one_record_suffix_reads_only_new_data(self) -> None:
        import copy as _copy

        import repository as repository_module
        from object_store import Create, Update
        from wal import canonical_json, event_id, record_fingerprint, sha256, validate_record

        fixture, repository = self._light_forge()
        repository_module.clear_repository_cache()
        repository_module.reset_repository_load_counts()
        manifest, _version, replay = repository.load()
        # Craft one valid competing-process generation via raw store writes:
        # a new descriptor plus a view_create record, then a new manifest.
        # This process's snapshot cache still holds the older generation.
        import identity as _identity

        source_id = next(
            view["descriptor_id"]
            for view in replay.active_views.values()
        )
        template = repository._descriptor(source_id)
        payload = {key: value for key, value in template.items() if key != "id"}
        payload["restrictions"] = _copy.deepcopy(payload["restrictions"])
        payload["restrictions"]["read"] = sorted(
            set(payload["restrictions"]["read"]) | {"shared/extra-deny"}
        )
        new_descriptor_id = _identity.compute_descriptor_id(payload)
        new_descriptor = {**payload, "id": new_descriptor_id}
        raw = repository.store
        prefix = f"repos/{repository.repository_id}"
        raw.put(
            f"{prefix}/descriptors/{new_descriptor_id}.json",
            canonical_json(new_descriptor),
            mode=Create(),
        )
        principal = template["principal_id"]
        view_payload = {
            "view_id": "carol",
            "principal_id": principal,
            "projected_ref": "refs/heads/main",
            "descriptor_id": new_descriptor_id,
            "descriptor_generation": 1,
            "projection_epoch": 1,
            "canonical_frontier": manifest.canonical_tip,
        }
        fingerprint = record_fingerprint(
            repository.repository_id, "view_create", view_payload
        )
        publication_id = "1" * 32
        record_key = (
            f"wal/records/{manifest.head_seq + 1:020d}"
            f"-{event_id(fingerprint)}-{publication_id}.json"
        )
        record = {
            "seq": manifest.head_seq + 1,
            "kind": "view_create",
            "event_fingerprint": fingerprint,
            "event_id": event_id(fingerprint),
            "publication_id": publication_id,
            "previous": {
                "seq": manifest.wal_head.seq,
                "key": manifest.wal_head.key,
                "sha256": manifest.wal_head.sha256,
            },
            "created_at": 1_700_000_000,
            "writer": "2" * 32,
            "payload": view_payload,
        }
        validate_record(record, repository_id=repository.repository_id)
        record_body = canonical_json(record)
        raw.put(f"{prefix}/{record_key}", record_body, mode=Create())
        advanced = dict(manifest.to_dict())
        advanced["head_seq"] = manifest.head_seq + 1
        advanced["revision"] = manifest.revision + 1
        advanced["wal_head"] = {
            "seq": record["seq"],
            "key": record_key,
            "sha256": sha256(record_body),
            "event_id": record["event_id"],
            "publication_id": publication_id,
        }
        current = raw.head(f"{prefix}/manifest.json")
        assert current is not None
        raw.put(f"{prefix}/manifest.json", canonical_json(advanced), mode=Update(current.version))
        before = dict(repository_module.repository_load_counts())
        from object_store_fs import FilesystemObjectStore

        real_read = FilesystemObjectStore._read
        body_read_keys: list[str] = []

        def counting(self, key: str):
            body_read_keys.append(key)
            return real_read(self, key)

        with patch.object(FilesystemObjectStore, "_read", counting):
            manifest2, _version2, replay2 = repository.load()
        counts = repository_module.repository_load_counts()
        self.assertEqual(manifest.head_seq + 1, manifest2.head_seq)
        self.assertEqual(before["full_loads"], counts["full_loads"])
        self.assertEqual(before["suffix_advances"] + 1, counts["suffix_advances"])
        # One new descriptor for the new view, fetched and validated once.
        self.assertEqual(
            before["descriptor_fetches"] + 1, counts["descriptor_fetches"]
        )
        self.assertIn("carol", replay2.active_views)
        # Bounded body reads: manifest + one WAL record + one descriptor.
        self.assertLessEqual(len(body_read_keys), 4)
        repository_module.clear_repository_cache()

    def _raw_overwrite_manifest(self, repository, body: bytes) -> None:
        """Overwrite the manifest through the raw store, bypassing eviction.

        Simulates a competing process advancing the manifest: this process's
        snapshot cache still holds the older generation.
        """
        from object_store import Update

        full_key = f"repos/{repository.repository_id}/manifest.json"
        current = repository.store.head(full_key)
        assert current is not None
        repository.store.put(full_key, body, mode=Update(current.version))

    def test_suffix_that_misses_the_cached_head_fails_closed(self) -> None:
        import repository as repository_module
        from wal import canonical_json

        _fixture, repository = self._light_forge()
        repository_module.clear_repository_cache()
        manifest, _version, _replay = repository.load()
        advanced = dict(manifest.to_dict())
        advanced["head_seq"] = manifest.head_seq + 1
        advanced["revision"] = manifest.revision + 1
        advanced["wal_head"] = {
            "seq": manifest.head_seq + 1,
            "key": f"wal/records/{manifest.head_seq + 1:020d}-{'0' * 64}-{'0' * 32}.json",
            "sha256": "0" * 64,
            "event_id": "0" * 64,
            "publication_id": "0" * 32,
        }
        self._raw_overwrite_manifest(repository, canonical_json(advanced))
        # The suffix walk reaches for a record that is missing: an integrity
        # failure, never the stale snapshot served as fallback.
        with self.assertRaises(ForgeError):
            repository.load()
        repository_module.clear_repository_cache()

    def test_non_monotonic_manifest_is_an_integrity_failure(self) -> None:
        import repository as repository_module
        from tests.identity_harness import add_view
        from wal import canonical_json

        fixture, repository = self._light_forge()
        repository_module.clear_repository_cache()
        manifest, _version, _replay = repository.load()
        add_view(fixture.root, "carol", ["shared"], ["shared"])
        manifest2, _version2, _replay2 = repository.load()
        self.assertEqual(manifest.head_seq + 1, manifest2.head_seq)
        # Rewind the manifest behind the cached head through a raw handle.
        self._raw_overwrite_manifest(repository, canonical_json(manifest.to_dict()))
        with self.assertRaisesRegex(ForgeError, "backwards|contiguous|head"):
            repository.load()
        repository_module.clear_repository_cache()

    def test_same_sequence_different_head_is_an_integrity_failure(self) -> None:
        import repository as repository_module
        from wal import canonical_json

        _fixture, repository = self._light_forge()
        repository_module.clear_repository_cache()
        manifest, _version, _replay = repository.load()
        tampered = dict(manifest.to_dict())
        tampered["wal_head"] = {
            **tampered["wal_head"],
            "key": tampered["wal_head"]["key"].replace("records/", "records/x"),
        }
        self._raw_overwrite_manifest(repository, canonical_json(tampered))
        with self.assertRaises(ForgeError):
            repository.load()
        repository_module.clear_repository_cache()

    def test_cache_eviction_falls_back_to_complete_replay(self) -> None:
        import repository as repository_module

        _fixture, repository = self._light_forge()
        repository_module.clear_repository_cache()
        repository_module.reset_repository_load_counts()
        repository.load()
        repository.load()
        self.assertEqual(1, repository_module.repository_load_counts()["full_loads"])
        repository_module.clear_repository_cache()
        repository.load()
        self.assertEqual(2, repository_module.repository_load_counts()["full_loads"])


class TargetedAccessTest(unittest.TestCase):
    def _traced(self, func, *args, **kwargs):
        import store as _store

        trace: list[str] = []
        previous = _store.QUERY_TRACE
        _store.QUERY_TRACE = trace
        try:
            result = func(*args, **kwargs)
        finally:
            _store.QUERY_TRACE = previous
        return result, trace

    @staticmethod
    def _mentions(trace: list[str], *tables: str) -> list[str]:
        hits = []
        for statement in trace:
            lowered = statement.lower()
            for table in tables:
                if table in lowered:
                    hits.append(statement)
                    break
        return hits

    def test_fetch_resolution_never_scans_history_or_job_tables(self) -> None:
        from http_server import _resolve_service
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        _result, trace = self._traced(_resolve_service, fixture.root, "alice")
        self.assertTrue(trace)
        self.assertEqual(
            [], self._mentions(trace, "transactions", "changes", "projection_jobs")
        )

    def test_browser_policy_loads_no_derived_history_or_jobs(self) -> None:
        from demo_server import _active_descriptor
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        _result, trace = self._traced(_active_descriptor, fixture.root, "alice")
        self.assertEqual(
            [], self._mentions(trace, "transactions", "changes", "projection_jobs")
        )

    def test_receive_resolution_reads_only_principal_provenance(self) -> None:
        from state import load_state_sections
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        _result, trace = self._traced(load_state_sections, fixture.root, ["principals"])
        self.assertEqual(
            [],
            self._mentions(
                trace,
                "transactions",
                "changes",
                "projection_jobs",
                "teams",
                "roles",
                "role_bindings",
                "view_descriptors",
                "views",
            ),
        )

    def test_saving_one_row_touches_no_unchanged_table(self) -> None:
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        store = open_store(fixture.root)
        try:
            baseline = store.load_document()
            state = json.loads(json.dumps(baseline))
            key = next(iter(state["views"]))
            state["views"][key] = {**state["views"][key], "probe": True}
            _result, trace = self._traced(
                store.save_document, state, [], None, baseline
            )
            self.assertEqual(
                [],
                self._mentions(
                    trace,
                    "transactions",
                    "changes",
                    "projection_jobs",
                    "principals",
                    "teams",
                    "roles",
                    "role_bindings",
                    "view_descriptors",
                    "forge_meta",
                ),
            )
            self.assertTrue(any("views" in item.lower() for item in trace))
        finally:
            store.close()

    def test_stale_scoped_write_fails_instead_of_overwriting(self) -> None:
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        first = open_store(fixture.root)
        second = open_store(fixture.root)
        try:
            base_a = first.load_document()
            base_b = second.load_document()
            key = next(iter(base_a["views"]))
            state_a = json.loads(json.dumps(base_a))
            state_a["views"][key] = {**state_a["views"][key], "writer": "a"}
            first.save_document(state_a, [], None, base_a)
            state_b = json.loads(json.dumps(base_b))
            state_b["views"][key] = {**state_b["views"][key], "writer": "b"}
            with self.assertRaisesRegex(ForgeError, "conflict"):
                second.save_document(state_b, [], None, base_b)
            # The winner's row survived; the stale delete also fails.
            current = first.load_document()
            self.assertEqual("a", current["views"][key]["writer"])
            state_c = json.loads(json.dumps(base_b))
            del state_c["views"][key]
            with self.assertRaisesRegex(ForgeError, "conflict"):
                second.save_document(state_c, [], None, base_b)
        finally:
            first.close()
            second.close()

    def test_noop_session_performs_no_derived_rebuild(self) -> None:
        from state import locked
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        with locked(fixture.root) as state:
            seq_before = state["last_indexed_seq"]
            _result, trace = self._traced(lambda: None)
            self.assertEqual([], trace)
        self.assertGreater(seq_before, 0)


class MaterializationWarmTest(unittest.TestCase):
    def test_warm_resolutions_skip_repeated_full_validation(self) -> None:
        import materialization as _mat
        from tests.storage_helpers import repository_state
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        repository_state(fixture.root)
        _mat.reset_materialization_counts()
        _mat.clear_materialization_registrations()
        (fixture.root / "materialization-receipts").mkdir(exist_ok=True)
        for path in (fixture.root / "materialization-receipts").glob("*.json"):
            path.unlink()
        manifest, replay = repository_state(fixture.root)
        from repository import Repository

        repository = Repository.open(fixture.root)
        _mat.ensure_view_materialized(fixture.root, "alice", manifest, replay, repository)
        first = _mat.materialization_counts()
        self.assertEqual(1, first["view_generations_built"])
        for _ in range(100):
            _mat.ensure_view_materialized(
                fixture.root, "alice", manifest, replay, repository
            )
        after = _mat.materialization_counts()
        self.assertEqual(first["view_generations_built"], after["view_generations_built"])
        self.assertEqual(first["full_view_validations"], after["full_view_validations"])
        self.assertGreaterEqual(after["light_view_checks"], 100)

    def test_restart_validates_once_and_reuses_the_receipt(self) -> None:
        import materialization as _mat
        from tests.storage_helpers import repository_state
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        from repository import Repository

        manifest, replay = repository_state(fixture.root)
        repository = Repository.open(fixture.root)
        _mat.ensure_view_materialized(fixture.root, "alice", manifest, replay, repository)
        # Simulate a process restart: registrations gone, receipts on disk.
        _mat.clear_materialization_registrations()
        _mat.reset_materialization_counts()
        first = _mat.ensure_view_materialized(
            fixture.root, "alice", manifest, replay, repository
        )
        second = _mat.ensure_view_materialized(
            fixture.root, "alice", manifest, replay, repository
        )
        self.assertEqual(first.path, second.path)
        counts = _mat.materialization_counts()
        self.assertEqual(1, counts["full_view_validations"])
        self.assertEqual(0, counts["view_generations_built"])

    def test_missing_receipt_rebuilds_and_tampering_fails_closed(self) -> None:
        import materialization as _mat
        from tests.storage_helpers import repository_state
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        from repository import Repository

        manifest, replay = repository_state(fixture.root)
        repository = Repository.open(fixture.root)
        materialized = _mat.ensure_view_materialized(
            fixture.root, "alice", manifest, replay, repository
        )
        receipts = fixture.root / "materialization-receipts"
        self.assertTrue((receipts / "alice.json").is_file())
        # Deleting receipts and caches is safe: verified reconstruction
        # from manifest-reachable data plus a fresh receipt. The on-disk
        # cursor alone is never trusted without an independently stored
        # expected tip, so a missing receipt rebuilds.
        (receipts / "alice.json").unlink()
        _mat.clear_materialization_registrations()
        _mat.reset_materialization_counts()
        rebuilt = _mat.ensure_view_materialized(
            fixture.root, "alice", manifest, replay, repository
        )
        counts = _mat.materialization_counts()
        self.assertEqual(1, counts["view_generations_built"])
        self.assertTrue((receipts / "alice.json").is_file())
        # A redirected hook cannot be trusted: rebuild or fail closed.
        hook = rebuilt.path / "hooks" / "pre-receive"
        hook.write_text("#!/bin/sh\nexit 0\n")
        hook.chmod(0o755)
        _mat.clear_materialization_registrations()
        healed = _mat.ensure_view_materialized(
            fixture.root, "alice", manifest, replay, repository
        )
        self.assertNotEqual(rebuilt.path, healed.path)
        from materialization import REJECT_ALL_HOOK

        self.assertEqual(REJECT_ALL_HOOK, (healed.path / "hooks" / "pre-receive").read_text())
        # A dangling object breaks closure: rebuild or fail closed.
        dangling = healed.path / "objects" / "ab" / ("c" * 38)
        dangling.parent.mkdir(parents=True, exist_ok=True)
        dangling.write_bytes(b"junk\n")
        _mat.clear_materialization_registrations()
        releveled = _mat.ensure_view_materialized(
            fixture.root, "alice", manifest, replay, repository
        )
        self.assertNotEqual(healed.path, releveled.path)
        _ = materialized


class WorkerBatchTest(unittest.TestCase):
    def test_drain_bounds_historical_work_per_generation(self) -> None:
        import repository as repository_module
        import worker as worker_module
        from repository import Repository
        from tests.identity_harness import add_view
        from tests.tree_harness import TreeFixture, build_commit, command

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        source = fixture.directory / "source-big"
        command("git", "init", "-q", "-b", "main", str(source))
        build_commit(
            source,
            None,
            {b"shared/base.txt": (b"100644", b"base\n")},
            b"initial\n",
            1_700_000_000,
        )
        import forge as forge_module

        many = fixture.directory / "many"
        forge_module.init_forge(many, source, "main")
        from tests import identity_harness

        identity_harness.provision_admin(many)
        for index in range(8):
            identity_harness.add_view(many, f"view{index}", ["shared"], ["shared"])
        repository_module.clear_repository_cache()
        repository_module.reset_repository_load_counts()
        worker_module.reset_worker_counters()
        from materialization import reset_materialization_counts

        reset_materialization_counts()
        completed = worker_module.drain_projection_jobs(many)
        self.assertEqual(8, completed)
        counts = worker_module.worker_counters()
        loads = repository_module.repository_load_counts()
        # Historical work is bounded per pinned generation, not multiplied by
        # job count: one recovery decision and at most one complete snapshot
        # acquisition however many views drain.
        self.assertEqual(1, counts["recovery_decisions"])
        self.assertLessEqual(loads["full_loads"], 1)
        self.assertEqual(1, counts["final_probes"])
        from state import load_state

        state = load_state(many)
        for index in range(8):
            job = state["projection_jobs"][f"project:view{index}"]
            self.assertEqual("completed", job["status"])
            self.assertEqual(
                state["last_indexed_seq"],
                state["views"][f"view{index}"]["materialized_seq"],
            )
        # Completed-but-stale jobs are re-targeted by the next batch: the
        # read-only peek must not mistake them for idle.
        decisions_before = worker_module.worker_counters()["recovery_decisions"]
        identity_harness.add_view(many, "view8", ["shared"], ["shared"])
        redrained = worker_module.drain_projection_jobs(many)
        self.assertEqual(9, redrained)
        self.assertEqual(
            decisions_before + 1, worker_module.worker_counters()["recovery_decisions"]
        )
        state = load_state(many)
        for index in range(9):
            name = f"view{index}"
            self.assertEqual("completed", state["projection_jobs"][f"project:{name}"]["status"])
            self.assertEqual(
                state["last_indexed_seq"], state["views"][name]["materialized_seq"]
            )

    def test_failed_view_does_not_roll_back_independent_views(self) -> None:
        import worker as worker_module
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        from state import load_state

        before = load_state(fixture.root)["last_indexed_seq"]
        self.assertGreater(before, 0)
        completed = worker_module.drain_projection_jobs(fixture.root)
        self.assertGreaterEqual(completed, 1)
        state = load_state(fixture.root)
        for view in ("alice", "bob", "limited"):
            job = state["projection_jobs"][f"project:{view}"]
            self.assertEqual("completed", job["status"])


class PostgresSafetyTest(unittest.TestCase):
    """PostgreSQL SQL-safety without a live server (identifier composition)."""

    def _fake_psycopg(self, handler):
        import types as _types

        calls: list[tuple] = []

        class FakeIdentifier:
            def __init__(self, name: str):
                self.name = name

        class FakeComposed:
            def __init__(self, template: str, identifiers: list[str]):
                self.template = template
                self.identifiers = list(identifiers)

        class FakeSQL:
            def __init__(self, template: str):
                self.template = template

            def format(self, *args):
                return FakeComposed(
                    self.template,
                    [arg.name if isinstance(arg, FakeIdentifier) else arg for arg in args],
                )

        fake_sql = _types.SimpleNamespace(Identifier=FakeIdentifier, SQL=FakeSQL)

        class FakeCursor:
            def __init__(self):
                self.description = [("key",), ("doc",), ("version",), ("checksum",)]

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, statement, parameters=()):
                calls.append((statement, parameters))
                return handler(statement, parameters, self)

            def fetchall(self):
                return handler.fetch_all()

            def fetchone(self):
                rows = handler.fetch_all()
                return rows[0] if rows else None

            def close(self):
                pass

        class FakeConnection:
            closed = False

            def __init__(self):
                self.info = _types.SimpleNamespace(transaction_status="IDLE")

            def cursor(self, name=None):
                return FakeCursor()

            def execute(self, statement):
                calls.append((statement, ()))

            def commit(self):
                calls.append(("COMMIT", ()))

            def rollback(self):
                calls.append(("ROLLBACK", ()))

            def close(self):
                pass

        fake_pq = _types.SimpleNamespace(
            TransactionStatus=_types.SimpleNamespace(IDLE="IDLE")
        )
        fake_module = _types.SimpleNamespace(
            connect=lambda dsn, **kwargs: handler.connect(dsn, FakeConnection),
            sql=fake_sql,
            pq=fake_pq,
        )
        return fake_module, fake_pq, calls

    def _patch_psycopg(self, fake_module, fake_pq):
        return patch.dict(
            sys.modules,
            {"psycopg": fake_module, "psycopg.sql": fake_module.sql, "psycopg.pq": fake_pq},
        )

    def _valid_tables(self, base: Path):
        import shutil as _shutil
        from store import _RESTORE_TABLE_ALLOWLIST

        tables = base / "tables"
        _shutil.rmtree(tables, ignore_errors=True)
        tables.mkdir(parents=True)
        doc = {
            "canonical_branch": "main",
            "repository_state": "ready",
            "repository_id": "a" * 32,
            "last_indexed_seq": 0,
        }
        import hashlib as _hashlib

        digest = _hashlib.sha256(
            json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        (tables / "forge_meta.ndjson").write_text(
            json.dumps({"key": 1, "doc": doc, "version": 1, "checksum": digest}) + "\n"
        )
        for table in _RESTORE_TABLE_ALLOWLIST:
            if table in {"forge_meta", "audit_log"}:
                continue
            (tables / f"{table}.ndjson").write_text("")
        (tables / "audit_log.ndjson").write_text(
            json.dumps(
                {
                    "seq": 1,
                    "kind": "k",
                    "subject": "s",
                    "detail": "d",
                    "at": 1.0,
                    "event_id": "evt-1",
                }
            )
            + "\n"
        )
        return tables

    def test_restore_never_interpolates_names_and_binds_values(self) -> None:
        from store import _PostgresBackend

        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        tables = self._valid_tables(base)
        # Hostile row values must travel as bound parameters, never SQL text.
        hostile_key = 'x"; DROP TABLE views;--'
        hostile_doc = {"marker": "'; DELETE FROM principals;--"}
        import hashlib as _hashlib

        digest = _hashlib.sha256(
            json.dumps(hostile_doc, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        with (tables / "views.ndjson").open("a") as handle:
            handle.write(
                json.dumps(
                    {"key": hostile_key, "doc": hostile_doc, "version": 1, "checksum": digest}
                )
                + "\n"
            )

        class Handler:
            def connect(self, dsn, connection):
                self.dsn = dsn
                return connection()

            def __call__(self, statement, parameters, cursor):
                return None

            def fetch_all(self):
                return [("audit_log_seq_seq",)]

        handler = Handler()
        fake_module, fake_pq, calls = self._fake_psycopg(handler)
        root = base / "forge"
        root.mkdir()
        backend = _PostgresBackend("postgresql://runtime@db.internal:5432/app", root, pooled=False)
        with self._patch_psycopg(fake_module, fake_pq):
            backend.restore(tables)
        templates = [
            statement.template if hasattr(statement, "template") else statement
            for statement, _parameters in calls
        ]
        # Every table reference is identifier-composed; no table name and no
        # hostile row value is interpolated into SQL text. Static code-owned
        # DDL is the only raw multi-table statement and never carries values.
        for statement, _parameters in calls:
            text = getattr(statement, "template", statement)
            if not isinstance(text, str):
                continue
            if text.startswith("CREATE"):
                continue
            if text == "DELETE FROM storage_locks":
                # Static lease-exclusion statement: no identifiers, no values.
                continue
            if "pg_get_serial_sequence" in text or "setval" in text or "pg_database_size" in text:
                continue
            for marker in ("views", "audit_log", "forge_meta", "principals", "storage_locks"):
                self.assertNotIn(marker, text, f"raw table name interpolated into SQL: {text!r}")
        joined_params = json.dumps([list(p) if isinstance(p, (list, tuple)) else p for _s, p in calls])
        # Hostile row content travels as bound parameters, never SQL text.
        self.assertIn("DROP TABLE views", joined_params)
        self.assertIn("DELETE FROM principals", joined_params)
        composed_tables: set[str] = set()
        for statement, _parameters in calls:
            if hasattr(statement, "identifiers"):
                composed_tables.update(
                    name for name in statement.identifiers if isinstance(name, str)
                )
        for expected in ("forge_meta", "views", "audit_log"):
            self.assertIn(expected, composed_tables)

    def test_provision_composes_the_database_identifier(self) -> None:
        from store import _PostgresBackend

        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        root = base / "forge"
        root.mkdir()

        seen: list[tuple] = []

        class Handler:
            def connect(self, dsn, connection):
                return connection()

            def __call__(self, statement, parameters, cursor):
                seen.append((statement, parameters))
                return None

            def fetch_all(self):
                return []

        handler = Handler()
        fake_module, fake_pq, calls = self._fake_psycopg(handler)
        backend = _PostgresBackend(
            "postgresql://runtime@db.internal:5432/app", root, pooled=False
        )
        with self._patch_psycopg(fake_module, fake_pq):
            backend.provision_database("postgresql://admin@db.internal:5432/postgres")
        self.assertTrue(calls)
        statement, _parameters = calls[0]
        self.assertTrue(hasattr(statement, "identifiers"))
        self.assertIn("CREATE DATABASE", statement.template)
        self.assertNotIn("bastion_", statement.template)
        self.assertEqual(1, len(statement.identifiers))

    def test_acquire_maps_transients_to_stable_errors(self) -> None:
        from store import _PostgresBackend, Store

        base = Path(tempfile.mkdtemp())
        self.addCleanup(_cleanup, base)
        root = base / "forge"
        root.mkdir()
        attempts = {"selects": 0}

        class Handler:
            def connect(self, dsn, connection):
                return connection()

            def __call__(self, statement, parameters, cursor):
                text = getattr(statement, "template", statement)
                if isinstance(text, str) and "SELECT" in text and "storage_locks" in text:
                    attempts["selects"] += 1
                    if attempts["selects"] == 1:
                        raise RuntimeError("could not serialize access due to concurrent update")
                    cursor.description = [("owner",), ("expires_at",)]
                    self._rows = []
                    return None
                if isinstance(text, str) and text.startswith("INSERT"):
                    cursor.rowcount = 1
                    return None
                cursor.rowcount = 0
                return None

            def fetch_all(self):
                return getattr(self, "_rows", [])

        handler = Handler()
        fake_module, fake_pq, _calls = self._fake_psycopg(handler)
        with self._patch_psycopg(fake_module, fake_pq):
            backend = _PostgresBackend(
                "postgresql://u@h/db", root, pooled=False
            )
            store = Store(backend)
            try:
                with patch.object(store_module, "LEASE_WAIT_SECONDS", 5.0):
                    expiry = store.acquire_leases(["s1"], "owner:1:2", root)
                self.assertGreater(expiry, time.time())
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
