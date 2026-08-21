"""Process harness, fixture builder, and oracle for real-process tests.

This module is intentionally not named ``test_*.py``.  It provides shared
building blocks for ``tests/test_process_concurrency.py`` and
``tests/test_process_failpoints.py``.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import forge
from tests import identity_harness
import worker
from errors import ForgeError
from git import clean_env
from invariants import check_invariants
from layout import canonical_repo, view_repo
from protocol import transaction_id
from state import load_state, process_start_time


LINUX = sys.platform.startswith("linux")
REPO_ROOT = Path(__file__).resolve().parent.parent
FORGE_PY = REPO_ROOT / "forge.py"
ACTOR_PY = REPO_ROOT / "tests" / "process_actor.py"


def skip_unless_linux(test: Any) -> Any:
    return unittest.skipUnless(
        LINUX and (Path("/proc") / "self" / "stat").exists(),
        "real-process tests require Linux /proc and flock",
    )(test)


def git_env() -> dict[str, str]:
    env = clean_env()
    env.update({
        "LC_ALL": "C",
        "LANG": "C",
    })
    return env


def run_git(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    env: dict[str, str] | None = None,
    data: bytes | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        input=data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ | (env or git_env()),
    )
    if check and result.returncode:
        raise AssertionError(f"git {' '.join(args)}\n{result.stdout}\n{result.stderr}")
    return result


def git_output(*args: str, cwd: Path | None = None) -> str:
    return run_git(*args, cwd=cwd).stdout.strip()


def commit_oid(repo: Path) -> str:
    return git_output("rev-parse", "HEAD", cwd=repo)


class Fixture:
    """One isolated forge with deterministic Alice/Bob/admin clones."""

    INITIAL: dict[str, bytes] = {
        "shared/base.txt": b"shared base\n",
        "team-a/base.txt": b"team a base\n",
        "team-b/base.txt": b"team b base\n",
        "hidden/secret.txt": b"hidden secret\n",
    }

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.forge = self.root / "forge"
        self.control = self.root / "control"
        self.control.mkdir()
        self._build()

    def _build(self) -> None:
        run_git("init", "-q", "-b", "main", "--object-format=sha1", str(self.source))
        self._configure(self.source, "Fixture", "fixture@example.test")
        for name, contents in self.INITIAL.items():
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        self._commit(self.source, "initial fixture", 1_700_000_000)
        forge.init_forge(self.forge, self.source, "main")
        self.admin_principal = identity_harness.provision_admin(self.forge)
        identity_harness.add_view(
            self.forge, "alice", ["shared", "team-a"], ["shared", "team-a"], "main"
        )
        identity_harness.add_view(
            self.forge, "bob", ["shared", "team-b"], ["shared", "team-b"], "main"
        )
        self.alice = self._clone("alice")
        self.bob = self._clone("bob")
        self.admin = self.root / "admin"
        run_git("clone", "-q", "--no-local", str(self.canonical), str(self.admin))
        self._configure(self.admin, "Administrator", "admin@example.test")

    def _configure(self, repo: Path, name: str, email: str) -> None:
        run_git("config", "user.name", name, cwd=repo)
        run_git("config", "user.email", email, cwd=repo)
        run_git("config", "commit.gpgSign", "false", cwd=repo)
        run_git("config", "tag.gpgSign", "false", cwd=repo)

    def _clone(self, name: str) -> Path:
        checkout = self.root / name
        run_git("clone", "-q", "--no-local", str(self.view_repo(name)), str(checkout))
        self._configure(checkout, name.title(), f"{name}@example.test")
        return checkout

    def extra_clone(self, name: str, suffix: str) -> Path:
        checkout = self.root / f"{name}-{suffix}"
        run_git("clone", "-q", "--no-local", str(self.view_repo(name)), str(checkout))
        self._configure(checkout, name.title(), f"{name}@example.test")
        return checkout

    def _commit(
        self,
        repo: Path,
        message: str,
        timestamp: int,
        change_id: str | None = None,
    ) -> str:
        if change_id:
            message = f"{message}\n\nChange-Id: {change_id}"
        env = {
            "GIT_AUTHOR_DATE": f"{timestamp} +0000",
            "GIT_COMMITTER_DATE": f"{timestamp} +0000",
        }
        run_git("add", "-A", cwd=repo)
        run_git("commit", "-q", "-m", message, cwd=repo, env=os.environ | env)
        return commit_oid(repo)

    @property
    def canonical(self) -> Path:
        return canonical_repo(self.forge)

    def view_repo(self, name: str) -> Path:
        return view_repo(self.forge, name)

    def state(self) -> dict:
        return load_state(self.forge)

    def client_commit(
        self,
        name: str,
        path: str,
        contents: bytes,
        message: str,
        timestamp: int,
        change_id: str | None = None,
    ) -> str:
        checkout = self.alice if name == "alice" else self.bob
        return self.commit_in(checkout, path, contents, message, timestamp, change_id)

    def commit_in(
        self,
        checkout: Path,
        path: str,
        contents: bytes,
        message: str,
        timestamp: int,
        change_id: str | None = None,
    ) -> str:
        target = checkout / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        return self._commit(checkout, message, timestamp, change_id)

    def admin_edit(
        self,
        path: str,
        contents: bytes,
        message: str,
        timestamp: int,
    ) -> str:
        target = self.admin / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        return self._commit(self.admin, message, timestamp)

    def stage_admin_commit(
        self,
        path: str,
        contents: bytes,
        message: str,
        timestamp: int,
    ) -> tuple[str, str]:
        """Push an administrative commit to canonical staging; return (old, new)."""
        old = git_output("rev-parse", "refs/heads/main", cwd=self.canonical)
        target = self.admin / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        new = self._commit(self.admin, message, timestamp)
        run_git(
            "push",
            "-q",
            str(self.canonical),
            f"{new}:refs/staging/admin-update",
            cwd=self.admin,
        )
        return old, new

    def admin_update(
        self,
        path: str,
        contents: bytes,
        message: str,
        timestamp: int,
    ) -> tuple[str, str]:
        """Place an administrative canonical update and return (old, new)."""
        old, new = self.stage_admin_commit(path, contents, message, timestamp)
        identity_harness.update_canonical(self.forge, old, new, self.admin_principal)
        return old, new

    def source_push(
        self,
        name: str,
        path: str,
        contents: bytes,
        message: str,
        timestamp: int,
        change_id: str | None = None,
    ) -> tuple[str, str]:
        checkout = self.alice if name == "alice" else self.bob
        old = commit_oid(checkout)
        target = checkout / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        new = self._commit(checkout, message, timestamp, change_id)
        run_git("push", "-q", "origin", "main", cwd=checkout)
        return old, new

    def recover(self) -> None:
        from state import locked

        with locked(self.forge):
            pass

    def run_worker(self, now: int) -> bool:
        return worker.run_projection_job(self.forge, now=now)

    def settle(self) -> None:
        """Run recovery and deterministic workers until nothing remains."""
        self.recover()
        now = 1_700_000_000
        while True:
            if not self.run_worker(now):
                break
            now += 1
        self.recover()

    def cleanup(self) -> None:
        self.temporary.cleanup()


@dataclass
class Actor:
    process: subprocess.Popen[str]
    marker_path: Path | None
    args: list[str]
    checkpoint: str | None
    marker: dict[str, Any] = field(default_factory=dict)
    signal_sent: str | None = None
    stdout: str = ""
    stderr: str = ""


class Controller:
    FORGE_PY = FORGE_PY
    ACTOR_PY = ACTOR_PY

    def __init__(self, fixture: Fixture, testcase: unittest.TestCase) -> None:
        self.fixture = fixture
        self.testcase = testcase
        self._counter = 0

    def _next_marker(self) -> Path:
        self._counter += 1
        return self.fixture.control / f"marker-{self._counter}.json"

    def start_actor(
        self,
        args: list[str],
        checkpoint: str | None = None,
        extra_env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> Actor:
        env = os.environ | git_env() | (extra_env or {})
        marker_path: Path | None = None
        if checkpoint:
            marker_path = self._next_marker()
            env = env | {
                "BASTION_TEST_FAILPOINT": checkpoint,
                "BASTION_TEST_FAILPOINT_ROOT": str(self.fixture.forge.resolve()),
                "BASTION_TEST_FAILPOINT_MARKER": str(marker_path),
            }
        process = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
            cwd=cwd,
        )
        return Actor(process, marker_path, args, checkpoint)

    def wait_marker(self, actor: Actor, timeout: float = 10.0) -> dict[str, Any]:
        if not actor.marker_path or not actor.checkpoint:
            raise AssertionError("actor has no checkpoint")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if actor.marker_path.exists():
                break
            if actor.process.poll() is not None:
                self._collect(actor)
                raise AssertionError(
                    f"actor exited before reaching {actor.checkpoint}: "
                    f"rc={actor.returncode}\n{actor.stderr}"
                )
            time.sleep(0.01)
        else:
            self._collect(actor)
            raise AssertionError(
                f"timeout waiting for checkpoint {actor.checkpoint}: "
                f"rc={actor.returncode}\n{actor.stderr}"
            )
        payload = json.loads(actor.marker_path.read_text())
        if payload.get("version") != 1:
            raise AssertionError(f"unexpected marker version: {payload}")
        if payload.get("checkpoint") != actor.checkpoint:
            raise AssertionError(
                f"checkpoint mismatch: wanted {actor.checkpoint}, got {payload}"
            )
        if payload.get("root") != str(self.fixture.forge.resolve()):
            raise AssertionError(f"marker root mismatch: {payload}")
        pid = payload.get("pid")
        if not isinstance(pid, int):
            raise AssertionError(f"marker pid invalid: {payload}")
        expected_start = payload.get("process_start")
        actual_start = process_start_time(pid)
        if actual_start != expected_start:
            raise AssertionError(
                f"process start identity mismatch for pid {pid}: "
                f"expected {expected_start}, got {actual_start}"
            )
        self._wait_stopped(pid, deadline)
        actor.marker = payload
        return payload

    def _wait_stopped(self, pid: int, deadline: float) -> None:
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{pid}/stat").read_text().split()[2]
            except (OSError, IndexError):
                return
            if state in {"T", "t"}:
                return
            time.sleep(0.01)
        raise AssertionError(f"process {pid} did not enter stopped state")

    def resume(self, actor: Actor) -> None:
        pid = self._marker_pid(actor)
        actor.signal_sent = "SIGCONT"
        os.kill(pid, signal.SIGCONT)

    def kill(self, actor: Actor) -> None:
        pid = self._marker_pid(actor)
        actor.signal_sent = "SIGKILL"
        os.kill(pid, signal.SIGKILL)

    def _marker_pid(self, actor: Actor) -> int:
        if not actor.marker:
            raise AssertionError("no marker available")
        return int(actor.marker["pid"])

    def finish(self, actor: Actor, timeout: float = 20.0) -> int:
        try:
            actor.stdout, actor.stderr = actor.process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._kill_group(actor)
            actor.stdout, actor.stderr = actor.process.communicate()
        actor.returncode = actor.process.returncode
        return actor.returncode

    def _collect(self, actor: Actor) -> None:
        try:
            actor.stdout, actor.stderr = actor.process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            self._kill_group(actor)
            actor.stdout, actor.stderr = actor.process.communicate()
        actor.returncode = actor.process.returncode

    def _kill_group(self, actor: Actor) -> None:
        try:
            os.killpg(actor.process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def cleanup(self, actor: Actor) -> None:
        if actor.process.poll() is None:
            self._kill_group(actor)
            try:
                actor.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        for stream in (actor.process.stdout, actor.process.stderr):
            if stream is not None:
                stream.close()

    def run_actor(
        self,
        args: list[str],
        checkpoint: str | None = None,
        extra_env: dict[str, str] | None = None,
        action: str | None = "SIGCONT",
        timeout: float = 20.0,
        cwd: Path | None = None,
    ) -> Actor:
        actor = self.start_actor(
            args, checkpoint=checkpoint, extra_env=extra_env, cwd=cwd
        )
        self.testcase.addCleanup(self.cleanup, actor)
        if checkpoint:
            self.wait_marker(actor)
            if action == "SIGCONT":
                self.resume(actor)
            elif action == "SIGKILL":
                self.kill(actor)
            else:
                raise AssertionError(f"unknown action: {action}")
        self.finish(actor, timeout=timeout)
        return actor

    def diagnostic(self, actor: Actor) -> str:
        lines = [
            f"actor args: {' '.join(actor.args)}",
            f"checkpoint: {actor.checkpoint}",
            f"signal sent: {actor.signal_sent}",
            f"return code: {getattr(actor, 'returncode', None)}",
            "stdout:",
            actor.stdout,
            "stderr:",
            actor.stderr,
        ]
        return "\n".join(lines)


# -----------------------------------------------------------------------------
# Authoritative snapshot and recovery-equivalence oracle
# -----------------------------------------------------------------------------


def _history(repo: Path, tip: str) -> list[str]:
    if tip == "0" * 40 or not tip:
        return []
    return git_output("rev-list", "--reverse", "--first-parent", tip, cwd=repo).splitlines()


def _tree(repo: Path, commit: str) -> dict[bytes, tuple[bytes, bytes]]:
    result: dict[bytes, tuple[bytes, bytes]] = {}
    for record in run_git("ls-tree", "-rz", "--full-tree", commit, cwd=repo).stdout.split("\0"):
        if not record:
            continue
        metadata, path = record.split("\t", 1)
        mode, kind, oid = metadata.split(" ")
        if kind != "blob":
            continue
        contents = run_git("cat-file", "blob", oid, cwd=repo).stdout.encode("utf-8", "surrogateescape")
        result[path.encode("utf-8", "surrogateescape")] = (mode.encode(), contents)
    return result


def _logical_transaction_key(tx: dict) -> tuple:
    if tx.get("kind") == "canonical_update":
        return ("admin", tx["canonical_old"], tx["canonical_new"])
    return ("source", tx["view"], tx["projected_old"], tx["projected_new"])


def _job_logical_key(tx_key: tuple, job: dict) -> tuple:
    return ("delivery", tx_key, job["view"], job["canonical_to"])


def authoritative_snapshot(root: Path, losing: tuple[str, str, str] | None = None) -> dict:
    state = load_state(root)
    canonical = canonical_repo(root)
    canonical_name = f"refs/heads/{state['canonical_branch']}"
    canonical_tip = git_output("rev-parse", canonical_name, cwd=canonical)

    snapshot: dict[str, Any] = {
        "canonical_tip": canonical_tip,
        "canonical_history": _history(canonical, canonical_tip),
        "canonical_tree": _tree(canonical, canonical_tip),
        "views": {},
        "changes": {},
        "source_mappings": {},
        "transaction_status": {},
        "job_status": {},
        "live_receivers": [],
        "blocked_events": [],
        "unfinished_jobs": [],
        "pending_views": [],
    }

    for name, view in sorted(state["views"].items()):
        repo = view_repo(root, name)
        tip = git_output("rev-parse", canonical_name, cwd=repo)
        snapshot["views"][name] = {
            "tip": tip,
            "history": _history(repo, tip),
            "tree": _tree(repo, tip),
            "canonical_frontier": view["canonical_frontier"],
            "projected_tip": view["projected_tip"],
            "anchors": dict(sorted(view.get("anchors", {}).items())),
        }

    for change_id, change in sorted(state.get("changes", {}).items()):
        revisions = [
            (
                rev.get("view"),
                rev.get("projected"),
                rev.get("canonical"),
                rev.get("kind"),
                rev.get("descriptor_id"),
                rev.get("descriptor_generation"),
            )
            for rev in change.get("revisions", [])
        ]
        snapshot["changes"][change_id] = {
            "owner": change.get("owner_principal_id"),
            "canonical_ref": change.get("canonical_ref"),
            "revisions": sorted(revisions),
        }

    for txid, tx in sorted(state.get("transactions", {}).items()):
        key = _logical_transaction_key(tx)
        if tx.get("kind") != "canonical_update":
            snapshot["source_mappings"][key] = [
                (m["submitted"], m["canonical"], m["change_id"])
                for m in tx.get("mappings", [])
            ]
        if tx["status"] != "aborted":
            snapshot["transaction_status"][key] = tx["status"]

    for job in state.get("projection_jobs", {}).values():
        tx = state["transactions"].get(job["transaction_id"])
        if tx:
            key = _job_logical_key(_logical_transaction_key(tx), job)
            snapshot["job_status"][key] = job["status"]
        if job["status"] != "completed":
            snapshot["unfinished_jobs"].append(job["id"])

    for event in state.get("outbox", {}).values():
        if event["status"] == "blocked":
            snapshot["blocked_events"].append(event["id"])

    for txid, tx in state.get("transactions", {}).items():
        if tx.get("kind") != "canonical_update" and tx["status"] in {
            "fanout_pending",
            "published",
        }:
            for view in tx.get("pending_views", []):
                snapshot["pending_views"].append((txid, view))

    receivers = root / "receivers"
    if receivers.exists():
        for marker in receivers.iterdir():
            snapshot["live_receivers"].append(marker.name)

    if losing:
        view, projected_new, change_id = losing
        snapshot["losing"] = {
            "view": view,
            "projected_new": projected_new,
            "change_id": change_id,
        }

    return snapshot


def _normalize_history(history: list[str]) -> list[str]:
    return history


def assert_equivalent(
    recovered: dict,
    control: dict,
    testcase: unittest.TestCase,
) -> None:
    """Compare two authoritative snapshots, allowing durable failure evidence."""
    testcase.assertEqual(recovered["canonical_tip"], control["canonical_tip"])
    testcase.assertEqual(
        recovered["canonical_history"], control["canonical_history"]
    )
    testcase.assertEqual(recovered["canonical_tree"], control["canonical_tree"])

    testcase.assertEqual(
        set(recovered["views"].keys()), set(control["views"].keys())
    )
    for name in control["views"]:
        rview = recovered["views"][name]
        cview = control["views"][name]
        testcase.assertEqual(rview["tip"], cview["tip"], f"view {name} tip differs")
        testcase.assertEqual(
            rview["history"], cview["history"], f"view {name} history differs"
        )
        testcase.assertEqual(
            rview["tree"], cview["tree"], f"view {name} tree differs"
        )
        testcase.assertEqual(
            rview["canonical_frontier"],
            cview["canonical_frontier"],
            f"view {name} frontier differs",
        )
        testcase.assertEqual(
            rview["projected_tip"],
            cview["projected_tip"],
            f"view {name} projected tip differs",
        )
        testcase.assertEqual(
            rview["anchors"], cview["anchors"], f"view {name} anchors differ"
        )

    testcase.assertEqual(recovered["changes"], control["changes"])
    testcase.assertEqual(recovered["source_mappings"], control["source_mappings"])
    testcase.assertEqual(
        recovered["transaction_status"], control["transaction_status"]
    )
    testcase.assertEqual(recovered["job_status"], control["job_status"])

    testcase.assertEqual(recovered["blocked_events"], [])
    testcase.assertEqual(recovered["unfinished_jobs"], [])
    testcase.assertEqual(recovered["live_receivers"], [])
    testcase.assertEqual(recovered["pending_views"], [])

    if "losing" in control:
        losing = control["losing"]
        view = losing["view"]
        projected_new = losing["projected_new"]
        change_id = losing["change_id"]
        testcase.assertNotIn(
            projected_new, control["views"][view]["history"]
        )
        testcase.assertNotIn(
            projected_new,
            [m[0] for m in control["source_mappings"].get(("source", view, "", projected_new), [])],
        )
        if change_id:
            testcase.assertTrue(
                change_id not in control["changes"]
                or not any(
                    rev[0] == view and rev[1] == projected_new
                    for rev in control["changes"][change_id]["revisions"]
                ),
                f"losing Change-Id {change_id} leaked",
            )


def stable_check(
    root: Path,
    testcase: unittest.TestCase,
    control: dict | None = None,
    losing: tuple[str, str, str] | None = None,
) -> dict:
    check_invariants(root)
    snapshot = authoritative_snapshot(root, losing=losing)
    if control is not None:
        assert_equivalent(snapshot, control, testcase)
    return snapshot
