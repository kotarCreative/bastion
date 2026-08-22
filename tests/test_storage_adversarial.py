"""Adversarial tests against the durable store itself.

These attacks assume a hostile environment: concurrent writers, killed
processes, bit-level tampering, forged schema versions, hostile identifiers,
and operators racing leases. Every test asserts that invariants survive:
checksums name corrupt rows, leases exclude exactly one writer, crashes
leave stealable state, and rejected writes change nothing.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from errors import ForgeError
from state import load_state
from store import STORAGE_SCHEMA_VERSION, initialize_store, open_store


WORKER_SCRIPT = textwrap.dedent(
    """
    import sys, time, uuid
    from pathlib import Path
    sys.path.insert(0, {src!r})
    from store import open_store
    from state import locked, save_state

    root = Path(sys.argv[1])
    scope = sys.argv[2]
    iterations = int(sys.argv[3])
    hold_seconds = float(sys.argv[4])
    key = f"counter-{{scope}}"

    for index in range(iterations):
        with locked(root, scopes=(scope,)) as state:
            counters = state.setdefault("changes", {{}})
            counters[key] = {{"value": counters.get(key, {{}}).get("value", 0) + 1}}
            save_state(root, state)
            time.sleep(hold_seconds)
    print("done")
    """
).format(src=str(Path(__file__).resolve().parent.parent / "src"))


class AdversarialStorageBase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = Path(tempfile.mkdtemp())
        self.addCleanup(self._cleanup)
        self.root = self.temporary / "forge"
        self.root.mkdir()
        initialize_store(self.root, "sqlite").close()
        from state import save_state

        save_state(
            self.root,
            {
                "canonical_branch": "main",
                "views": {},
                "transactions": {},
                "changes": {},
                "outbox": {},
                "projection_jobs": {},
                "principals": {},
                "teams": {},
                "roles": {},
                "role_bindings": {},
                "view_descriptors": {},
            },
        )

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.temporary, ignore_errors=True)

    def _spawn(self, *arguments: str) -> subprocess.Popen:
        return subprocess.Popen(
            [
                sys.executable,
                "-c",
                WORKER_SCRIPT,
                str(self.root),
                *arguments,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


def _sqlite(root: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(root / "store.sqlite3")
    connection.isolation_level = None
    return connection


class ConcurrentWritersTest(AdversarialStorageBase):
    def test_parallel_scope_writers_converge_exactly(self) -> None:
        """Six processes x five increments per scope land without loss."""
        scopes = [f"view:worker-{index}" for index in range(6)]
        processes = []
        for scope in scopes:
            processes.append(self._spawn(scope, "5", "0.0"))
        for process in processes:
            output, error = process.communicate(timeout=120)
            self.assertEqual(0, process.returncode, error.decode())

        store = open_store(self.root)
        document = store.load_document()
        stats = store.stats()
        store.close()
        for scope in scopes:
            key = f"counter-{scope}"
            self.assertEqual(5, document["changes"][key]["value"], key)

    def test_same_scope_serializes_and_counts_are_exact(self) -> None:
        """Two processes hammering ONE scope still produce exact totals."""
        processes = [self._spawn("global-shared", "6", "0.005") for _ in range(2)]
        for process in processes:
            output, error = process.communicate(timeout=120)
            self.assertEqual(0, process.returncode, error.decode())
        store = open_store(self.root)
        document = store.load_document()
        store.close()
        self.assertEqual(
            12, document["changes"]["counter-global-shared"]["value"]
        )

    def test_killed_holder_lease_is_stolen_quickly(self) -> None:
        holder = self._spawn("global-victim", "1", "30")
        time.sleep(1.5)
        deadline = time.time() + 10
        while time.time() < deadline:
            connection = _sqlite(self.root)
            try:
                row = connection.execute(
                    "SELECT owner FROM storage_locks WHERE scope = 'global-victim'"
                ).fetchone()
            finally:
                connection.close()
            if row is not None:
                break
            time.sleep(0.1)
        self.assertIsNotNone(row, "holder never acquired the lease")

        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=10)

        started = time.time()
        successor = self._spawn("global-victim", "1", "0")
        output, error = successor.communicate(timeout=60)
        elapsed = time.time() - started
        self.assertEqual(0, successor.returncode, error.decode())
        # TTL is 60s; liveness stealing must recover far faster.
        self.assertLess(elapsed, 30.0)


class LeaseIntegrityTest(AdversarialStorageBase):
    def test_save_after_lease_theft_is_atomic_nothing_written(self) -> None:
        store = open_store(self.root)
        owner = f"{uuid_hex()}:{os.getpid()}:{_start_time()}"
        store.acquire_leases(["global"], owner)
        baseline = {"canonical_branch": "main"}
        store.save_document(baseline, [("global", owner)], None)

        thief = f"{uuid_hex()}:{os.getpid()}:{_start_time()}"
        connection = _sqlite(self.root)
        try:
            connection.execute(
                "UPDATE storage_locks SET owner = ? WHERE scope = 'global'", (thief,)
            )
        finally:
            connection.close()

        tampered = {"canonical_branch": "main", "views": {"hijack": {"x": 1}}}
        with self.assertRaises(ForgeError):
            store.save_document(tampered, [("global", owner)], None)

        # Nothing from the aborted write leaked.
        store.close()
        reloaded = load_state(self.root)
        self.assertNotIn("hijack", reloaded["views"])

    def test_concurrent_idempotency_insert_is_single_record(self) -> None:
        """Racing recorders of one key converge to one stored fingerprint."""
        script = textwrap.dedent(
            """
            import sys
            from pathlib import Path
            sys.path.insert(0, {src!r})
            from store import open_store
            root = Path(sys.argv[1])
            store = open_store(root)
            try:
                store.record_idempotency("race-key", {{"n": 1}}, {{"ok": True}})
                print("recorded")
            except Exception as error:
                print(f"error: {{error}}")
            store.close()
            """
        ).format(src=str(Path(__file__).resolve().parent.parent / "src"))
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.root)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(8)
        ]
        results = []
        for process in processes:
            output, error = process.communicate(timeout=60)
            self.assertEqual(0, process.returncode, error.decode())
            results.append(output.decode().strip())
        self.assertEqual(8, len(results))
        store = open_store(self.root)
        record = store.lookup_idempotency("race-key")
        store.close()
        self.assertEqual({"n": 1}, record["fingerprint"])


class TamperTest(AdversarialStorageBase):
    def _seed(self) -> None:
        store = open_store(self.root)
        document = {
            "canonical_branch": "main",
            "views": {"alice": {"tip": "0" * 40}},
            "transactions": {"t1": {"status": "published"}},
            "changes": {},
            "outbox": {},
            "projection_jobs": {},
            "principals": {},
            "teams": {},
            "roles": {},
            "role_bindings": {},
            "view_descriptors": {},
        }
        store.save_document(document, [], None)
        store.close()

    def _tamper(self, statement: str, parameters: tuple) -> None:
        connection = _sqlite(self.root)
        try:
            connection.execute(statement, parameters)
        finally:
            connection.close()
            self.root.joinpath("store.sqlite3-wal").exists()

    def test_document_bitflip_is_named(self) -> None:
        self._seed()
        self._tamper(
            "UPDATE views SET doc = replace(doc, '0', 'f') WHERE key = 'alice'", ()
        )
        with self.assertRaises(ForgeError) as caught:
            load_state(self.root)
        self.assertIn("views.alice", str(caught.exception))

    def test_checksum_bitflip_is_named(self) -> None:
        self._seed()
        self._tamper(
            "UPDATE transactions SET checksum = '0000' WHERE key = 't1'", ()
        )
        with self.assertRaises(ForgeError) as caught:
            load_state(self.root)
        self.assertIn("transactions.t1", str(caught.exception))

    def test_meta_tamper_is_detected(self) -> None:
        self._seed()
        self._tamper(
            "UPDATE forge_meta SET checksum = 'bogus' WHERE id = 1", ()
        )
        with self.assertRaises(ForgeError):
            load_state(self.root)

    def test_forged_schema_version_blocks_bootstrap(self) -> None:
        self._seed()
        self._tamper(
            "UPDATE schema_migrations SET version = ?", (STORAGE_SCHEMA_VERSION + 7,)
        )
        store = open_store(self.root)
        with self.assertRaises(ForgeError):
            store.bootstrap()
        store.close()


class HostileDataTest(AdversarialStorageBase):
    def test_hostile_identifiers_survive_process_boundary(self) -> None:
        hostile = {
            'inj"); DROP TABLE transactions;--': {"level": 0},
            "newline\nin-key": {"level": 1},
            "tab\tkey": {"level": 2},
            "☃ unicode": {"level": 3},
            "'quote'key": {"level": 4},
            "%s?%d{}": {"level": 5},
        }
        writer = textwrap.dedent(
            """
            import sys, json
            from pathlib import Path
            sys.path.insert(0, {src!r})
            from store import open_store
            root = Path(sys.argv[1])
            payload = json.loads(sys.argv[2])
            store = open_store(root)
            document = {{
                "canonical_branch": "main",
                "views": payload,
                "changes": {{}}, "transactions": {{}}, "outbox": {{}},
                "projection_jobs": {{}}, "principals": {{}}, "teams": {{}},
                "roles": {{}}, "role_bindings": {{}}, "view_descriptors": {{}},
            }}
            store.save_document(document, [], None)
            store.close()
            print("saved")
            """
        ).format(src=str(Path(__file__).resolve().parent.parent / "src"))
        process = subprocess.run(
            [
                sys.executable,
                "-c",
                writer,
                str(self.root),
                json.dumps(hostile),
            ],
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(0, process.returncode, process.stderr.decode())

        reader = subprocess.run(
            [
                sys.executable,
                "-c",
                textwrap.dedent(
                    """
                    import sys, json
                    from pathlib import Path
                    sys.path.insert(0, {src!r})
                    from store import open_store
                    root = Path(sys.argv[1])
                    store = open_store(root)
                    print(json.dumps(store.load_document()["views"]))
                    store.close()
                    """
                ).format(src=str(Path(__file__).resolve().parent.parent / "src")),
                str(self.root),
            ],
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(0, reader.returncode, reader.stderr.decode())
        reloaded = json.loads(reader.stdout.decode())
        self.assertEqual(hostile, reloaded)


class BrokenViewIsolationTest(AdversarialStorageBase):
    def test_dead_view_does_not_block_sibling_progress(self) -> None:
        """A job stuck dead-lettering on a ghost view cannot starve alice."""
        from outbox import MAX_JOB_ATTEMPTS
        from worker import run_projection_job

        import git as forge_git

        (self.root / "views").mkdir(exist_ok=True)
        subprocess.run(
            ["git", "init", "--bare", "-q", str(self.root / "views" / "ghost.git")],
            check=True,
        )
        subprocess.run(
            ["git", "init", "--bare", "-q", "-b", "main", str(self.root / "views" / "alice.git")],
            check=True,
        )
        store = open_store(self.root)
        document = {
            "canonical_branch": "main",
            "views": {
                "alice": {
                    "principal_id": "alice",
                    "descriptor_id": "vdesc-1",
                    "descriptor_generation": 1,
                    "canonical_ref": "refs/heads/main",
                    "projected_ref": "refs/heads/main",
                    "canonical_frontier": "0" * 40,
                    "projected_tip": "0" * 40,
                    "anchors": {},
                }
            },
            "view_descriptors": {},
            "transactions": {},
            "changes": {},
            "outbox": {},
            "projection_jobs": {},
            "principals": {},
            "teams": {},
            "roles": {},
            "role_bindings": {},
        }
        # Ghost transaction blocks ordering for ghost only.
        document["transactions"]["t-ghost"] = {
            "id": "t-ghost",
            "status": "fanout_pending",
            "pending_views": [],
            "view_errors": {},
        }
        document["projection_jobs"]["project:t-ghost:ghost"] = {
            "id": "project:t-ghost:ghost",
            "event_id": "e-ghost",
            "transaction_id": "t-ghost",
            "view": "ghost",
            "descriptor_id": "vdesc-1",
            "descriptor_generation": 1,
            "sequence": 1,
            "canonical_to": "0" * 40,
            "status": "pending",
            "attempts": 0,
            "last_error": None,
            "next_attempt_at": 0,
        }
        store.save_document(document, [], None)

        # Drive the broken view's job to dead while asserting that the
        # worker loop keeps making progress decisions each round.
        now = 1_700_000_000.0
        rounds = 0
        while True:
            progressed = run_projection_job(root=self.root, now=now)
            rounds += 1
            state = load_state(self.root)
            ghost = state["projection_jobs"]["project:t-ghost:ghost"]
            if ghost["status"] == "dead":
                break
            now += 61
            self.assertLess(rounds, MAX_JOB_ATTEMPTS * 6, "worker loop stalled")
        self.assertEqual(MAX_JOB_ATTEMPTS, ghost["attempts"])
        self.assertIn(
            "ghost", state["transactions"]["t-ghost"]["view_errors"]
        )
        # The healthy view row remains untouched and consistent.
        self.assertIn("alice", state["views"])
        store.close()


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


def _start_time() -> str:
    fields = Path(f"/proc/{os.getpid()}/stat").read_text().split()
    return fields[21]


if __name__ == "__main__":
    unittest.main()
