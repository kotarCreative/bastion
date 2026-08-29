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
from store import initialize_store, open_store


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
        with locked(root, recoverer=lambda _root, _state: None, scopes=(scope,)) as state:
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
                "repository_state": "ready",
                "repository_id": "a" * 32,
                "last_indexed_seq": 0,
                "views": {},
                "transactions": {},
                "changes": {},
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
        baseline = store.load_document()
        store.save_document(baseline, [("global", owner)], None)

        thief = f"{uuid_hex()}:{os.getpid()}:{_start_time()}"
        connection = _sqlite(self.root)
        try:
            connection.execute(
                "UPDATE storage_locks SET owner = ? WHERE scope = 'global'", (thief,)
            )
        finally:
            connection.close()

        tampered = json.loads(json.dumps(baseline))
        tampered["views"] = {"hijack": {"x": 1}}
        with self.assertRaises(ForgeError):
            store.save_document(tampered, [("global", owner)], None)

        # Nothing from the aborted write leaked.
        store.close()
        reloaded = load_state(self.root)
        self.assertNotIn("hijack", reloaded["views"])

class TamperTest(AdversarialStorageBase):
    def _seed(self) -> None:
        store = open_store(self.root)
        document = {
            "canonical_branch": "main",
            "repository_state": "ready",
            "repository_id": "a" * 32,
            "last_indexed_seq": 0,
            "views": {"alice": {"tip": "0" * 40}},
            "transactions": {"t1": {"status": "published"}},
            "changes": {},
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
                "repository_state": "ready",
                "repository_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "last_indexed_seq": 0,
                "views": payload,
                "changes": {{}}, "transactions": {{}},
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


class BrokenViewIsolationTest(unittest.TestCase):
    def test_dead_view_does_not_block_sibling_progress(self) -> None:
        """A dead hint for one view cannot block another view's materialization."""
        import forge
        from state import save_state
        from tests.tree_harness import TreeFixture

        fixture = TreeFixture()
        self.addCleanup(fixture.close)
        state = load_state(fixture.root)
        state["projection_jobs"] = {
            "project:alice": {
                "view": "alice",
                "target_seq": 999,
                "status": "dead",
                "attempts": 8,
                "next_attempt_at": None,
                "last_error": "broken",
                "lease_token": None,
                "lease_expires_at": None,
            }
        }
        save_state(fixture.root, state)

        forge.view_repo(fixture.root, "bob").unlink()
        forge.sync_view(fixture.root, "bob")

        self.assertTrue(forge.view_repo(fixture.root, "bob").is_symlink())
        self.assertEqual(
            "dead", load_state(fixture.root)["projection_jobs"]["project:alice"]["status"]
        )


def uuid_hex() -> str:
    import uuid

    return uuid.uuid4().hex


def _start_time() -> str:
    fields = Path(f"/proc/{os.getpid()}/stat").read_text().split()
    return fields[21]


if __name__ == "__main__":
    unittest.main()
