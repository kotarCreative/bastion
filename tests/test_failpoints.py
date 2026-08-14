from __future__ import annotations

import contextlib
import json
import os
import signal
import tempfile
import time
import unittest
from pathlib import Path
from typing import Iterator

import failpoints


class FailpointCheckpointTest(unittest.TestCase):
    def test_unarmed_call_is_inert(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            failpoints.checkpoint(root, "ignored.point")
            self.assertFalse(any(root.iterdir()))

    def test_root_mismatch_is_inert(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            other = Path(temporary) / "other"
            env = {
                "BASTION_TEST_FAILPOINT": "root.point",
                "BASTION_TEST_FAILPOINT_ROOT": str(other.resolve()),
                "BASTION_TEST_FAILPOINT_MARKER": str(root / "marker.json"),
            }
            with self._env(env):
                failpoints.checkpoint(root, "root.point")
            self.assertFalse((root / "marker.json").exists())

    def test_name_mismatch_is_inert(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {
                "BASTION_TEST_FAILPOINT": "wanted.point",
                "BASTION_TEST_FAILPOINT_ROOT": str(root.resolve()),
                "BASTION_TEST_FAILPOINT_MARKER": str(root / "marker.json"),
            }
            with self._env(env):
                failpoints.checkpoint(root, "other.point")
            self.assertFalse((root / "marker.json").exists())

    def test_incomplete_configuration_fails_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {"BASTION_TEST_FAILPOINT": "only.name"}
            with self._env(env):
                with self.assertRaises(RuntimeError):
                    failpoints.checkpoint(root, "only.name")

    @unittest.skipUnless(
        hasattr(os, "fork") and hasattr(signal, "SIGSTOP"),
        "requires POSIX fork and SIGSTOP",
    )
    def test_marker_contains_version_and_identities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker.json"
            env = {
                "BASTION_TEST_FAILPOINT": "test.point",
                "BASTION_TEST_FAILPOINT_ROOT": str(root.resolve()),
                "BASTION_TEST_FAILPOINT_MARKER": str(marker),
            }
            pid = os.fork()
            if pid == 0:
                with self._env(env):
                    failpoints.checkpoint(root, "test.point")
                os._exit(0)

            self._wait_for_file(marker, 10.0)
            payload = self._read_marker(marker)
            self.assertEqual(1, payload["version"])
            self.assertEqual("test.point", payload["checkpoint"])
            self.assertEqual(str(root.resolve()), payload["root"])
            self.assertIsInstance(payload["pid"], int)
            self.assertEqual(
                failpoints._process_start_time(payload["pid"]),
                payload["process_start"],
            )
            os.kill(payload["pid"], signal.SIGCONT)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(0, os.waitstatus_to_exitcode(status))

    @unittest.skipUnless(
        hasattr(os, "fork") and hasattr(signal, "SIGSTOP"),
        "requires POSIX fork and SIGSTOP",
    )
    def test_duplicate_marker_is_a_harness_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker.json"
            marker.write_text("{}")
            env = {
                "BASTION_TEST_FAILPOINT": "test.point",
                "BASTION_TEST_FAILPOINT_ROOT": str(root.resolve()),
                "BASTION_TEST_FAILPOINT_MARKER": str(marker),
            }
            pid = os.fork()
            if pid == 0:
                try:
                    with self._env(env):
                        failpoints.checkpoint(root, "test.point")
                except FileExistsError:
                    os._exit(0)
                os._exit(1)

            _, status = os.waitpid(pid, 0)
            self.assertEqual(0, os.waitstatus_to_exitcode(status))

    @unittest.skipUnless(
        hasattr(os, "fork") and hasattr(signal, "SIGSTOP"),
        "requires POSIX fork and SIGSTOP",
    )
    def test_environment_is_cleared_after_match(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker.json"
            env = {
                "BASTION_TEST_FAILPOINT": "test.point",
                "BASTION_TEST_FAILPOINT_ROOT": str(root.resolve()),
                "BASTION_TEST_FAILPOINT_MARKER": str(marker),
            }
            pid = os.fork()
            if pid == 0:
                with self._env(env):
                    failpoints.checkpoint(root, "test.point")
                    self.assertIsNone(os.environ.get("BASTION_TEST_FAILPOINT"))
                os._exit(0)

            self._wait_for_file(marker, 10.0)
            payload = self._read_marker(marker)
            os.kill(payload["pid"], signal.SIGCONT)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(0, os.waitstatus_to_exitcode(status))

    @unittest.skipUnless(
        hasattr(os, "fork") and hasattr(signal, "SIGSTOP"),
        "requires POSIX fork and SIGSTOP",
    )
    def test_child_stops_and_resumes_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            marker = root / "marker.json"
            env = {
                "BASTION_TEST_FAILPOINT": "stop.point",
                "BASTION_TEST_FAILPOINT_ROOT": str(root.resolve()),
                "BASTION_TEST_FAILPOINT_MARKER": str(marker),
            }
            pid = os.fork()
            if pid == 0:
                with self._env(env):
                    failpoints.checkpoint(root, "stop.point")
                    failpoints.checkpoint(root, "stop.point")
                os._exit(0)

            self._wait_for_file(marker, 10.0)
            payload = self._read_marker(marker)
            self.assertIsInstance(payload["pid"], int)
            os.kill(payload["pid"], signal.SIGCONT)
            _, status = os.waitpid(pid, 0)
            self.assertEqual(0, os.waitstatus_to_exitcode(status))

    @contextlib.contextmanager
    def _env(self, values: dict[str, str | None]) -> Iterator[None]:
        saved = {key: os.environ.get(key) for key in values}
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            yield
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _read_marker(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def _wait_for_file(self, path: Path, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if path.exists():
                return
            time.sleep(0.01)
        self.fail(f"marker {path} did not appear within {timeout}s")


if __name__ == "__main__":
    unittest.main()
