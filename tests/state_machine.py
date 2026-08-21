"""Deterministic real-Git state-machine harness for the version-1 forge.

This module deliberately is not discovered as a test module.  The small public
surface is used by ``tests.test_state_machine`` and is also useful when reducing
a failing seed to its recorded operations.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import forge
from tests import identity_harness
import worker
from errors import ForgeError
from invariants import check_invariants
from service import build_projection_plan
from state import locked, load_state, save_state


PATHS = (
    "shared/a.txt", "shared/b.txt", "team-a/a.txt", "team-a/b.txt",
    "team-b/a.txt", "team-b/b.txt", "hidden/a.txt", "hidden/b.txt",
)
ADDABLE_VIEWS = (
    ("reader", ["shared"], []),
    ("team-a-bot", ["team-a"], ["team-a"]),
    ("shared-bot", ["shared"], ["shared"]),
)
WEIGHTS = {
    "admin_edit": 4, "client_refresh": 2, "authorized_push": 3,
    "unauthorized_push": 2, "stale_push": 1, "server_sync": 2,
    "add_view": 1, "worker_complete": 4,
}


@dataclass(frozen=True)
class Operation:
    kind: str
    arguments: dict[str, object]


@dataclass
class RunResult:
    seed: int
    steps: int
    trace: list[Operation]
    state_bytes: bytes
    refs: dict[str, dict[str, str]]


def clean_git_environment() -> dict[str, str]:
    """Return the harness's own Git isolation environment.

    It mirrors the production contract without importing production Git helpers,
    so the independent tree oracle cannot inherit their behavior.
    """
    environment = dict(os.environ)
    names = {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
        "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE", "GIT_NAMESPACE",
        "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX", "GIT_QUARANTINE_PATH", "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    }
    for name in list(environment):
        if name in names or name.startswith("GIT_CONFIG_KEY_") or name.startswith("GIT_CONFIG_VALUE_"):
            environment.pop(name, None)
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1", "LC_ALL": "C", "LANG": "C",
    })
    return environment


def command(*args: str, cwd: Path | None = None, check: bool = True,
            env: dict[str, str] | None = None, data: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(args, cwd=cwd, input=data, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=env or clean_git_environment())
    if check and result.returncode:
        raise AssertionError(f"{' '.join(args)}\n{result.stderr.decode(errors='replace')}")
    return result


def git(repo: Path, *args: str, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
    return command("git", *args, cwd=repo, **kwargs)  # type: ignore[arg-type]


def tree_at(repo: Path, commit: str) -> dict[bytes, tuple[bytes, bytes]]:
    """Read a complete regular-file tree with Git plumbing only."""
    result = git(repo, "ls-tree", "-rz", "--full-tree", commit)
    tree: dict[bytes, tuple[bytes, bytes]] = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, kind, oid = metadata.split(b" ")
        if kind != b"blob":
            raise AssertionError(f"unexpected non-blob entry {path!r}")
        contents = git(repo, "cat-file", "blob", oid.decode("ascii")).stdout
        tree[path] = (mode, contents)
    return tree


def under_prefix(path: bytes, prefixes: list[str]) -> bool:
    return any(not prefix or path == prefix.encode() or path.startswith(prefix.encode() + b"/") for prefix in prefixes)


def filtered(tree: dict[bytes, tuple[bytes, bytes]], prefixes: list[str]) -> dict[bytes, tuple[bytes, bytes]]:
    return {path: entry for path, entry in tree.items() if under_prefix(path, prefixes)}


class Scenario:
    """One isolated scenario, including its independent model and local clients."""

    def __init__(self, seed: int, steps: int, directory: Path) -> None:
        self.seed, self.steps, self.directory = seed, steps, directory
        self.random = random.Random(seed)
        self.environment = clean_git_environment()
        self.root = directory / "forge"
        self.source = directory / "source"
        self.admin = directory / "admin"
        self.trace: list[Operation] = []
        self.logical_now = 1_800_000_000
        self.trees: dict[str, dict[bytes, tuple[bytes, bytes]]] = {}
        self.canonical_tree: dict[bytes, tuple[bytes, bytes]] = {}
        self.views: dict[str, dict[str, object]] = {}
        self.clients: dict[str, dict[str, object]] = {}
        self.addable = list(ADDABLE_VIEWS)
        self.worker_crash: str | None = None
        self.source_crash: dict[str, object] | None = None
        self.edit_number = 0
        self._fixture()

    def _fixture(self) -> None:
        git(self.directory, "init", "-q", "--object-format=sha1", "-b", "main", str(self.source))
        self._configure(self.source, "Fixture", "fixture@example.test")
        initial = {
            "shared/base.txt": b"shared base\n", "team-a/base.txt": b"team a base\n",
            "team-b/base.txt": b"team b base\n", "hidden/secret.txt": b"hidden secret\n",
        }
        for name, contents in initial.items():
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(contents)
        initial_oid = self._commit(self.source, "initial fixture", 0)
        forge.init_forge(self.root, self.source, "main")
        identity_harness.provision_admin(self.root)
        identity_harness.add_view(self.root, "alice", ["shared", "team-a"], ["shared", "team-a"], "main")
        identity_harness.add_view(self.root, "bob", ["shared", "team-b"], ["shared", "team-b"], "main")
        self.canonical_tree = tree_at(forge.canonical_repo(self.root), initial_oid)
        self.trees[initial_oid] = dict(self.canonical_tree)
        for name, read, write in (("alice", ["shared", "team-a"], ["shared", "team-a"]),
                                  ("bob", ["shared", "team-b"], ["shared", "team-b"])):
            self.views[name] = {"read": read, "write": write}
            self._clone_client(name)
        git(self.directory, "clone", "-q", "--no-local", str(forge.canonical_repo(self.root)), str(self.admin))
        self._configure(self.admin, "Administrator", "admin@example.test")
        self.checkpoint()

    def _configure(self, repository: Path, name: str, email: str) -> None:
        git(repository, "config", "user.name", name)
        git(repository, "config", "user.email", email)
        git(repository, "config", "commit.gpgSign", "false")
        git(repository, "config", "tag.gpgSign", "false")

    def _clone_client(self, name: str) -> None:
        checkout = self.directory / f"client-{name}"
        git(self.directory, "clone", "-q", "--no-local", str(forge.view_repo(self.root, name)), str(checkout))
        self._configure(checkout, name.title(), f"{name}@example.test")
        oid = git(checkout, "rev-parse", "HEAD").stdout.decode().strip()
        self.clients[name] = {"path": checkout, "oid": oid, "tree": tree_at(checkout, oid), "unpushed": False, "clean": True}

    def _commit(self, repository: Path, message: str, step: int, index: int = 0) -> str:
        git(repository, "add", "-A")
        timestamp = 1_600_000_000 + step * 10 + index
        environment = self.environment | {"GIT_AUTHOR_DATE": f"{timestamp} +0000", "GIT_COMMITTER_DATE": f"{timestamp} +0000"}
        git(repository, "commit", "-q", "-m", message, env=environment)
        return git(repository, "rev-parse", "HEAD").stdout.decode().strip()

    def _change_id(self, step: int, index: int) -> str:
        return "I" + hashlib.sha1(f"{self.seed}:{step}:{index}".encode()).hexdigest()

    def _refs(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for name, repo in {"canonical": forge.canonical_repo(self.root), **{name: forge.view_repo(self.root, name) for name in self.views}}.items():
            output = git(repo, "for-each-ref", "--format=%(refname) %(objectname)").stdout.decode().splitlines()
            result[name] = {line.split()[0]: line.split()[1] for line in output}
        return result

    def _apply(self, tree: dict[bytes, tuple[bytes, bytes]], arguments: dict[str, object]) -> dict[bytes, tuple[bytes, bytes]]:
        result = dict(tree)
        path = str(arguments["path"]).encode()
        action = arguments["action"]
        if action == "delete":
            if path not in result:
                raise AssertionError(f"cannot delete absent {path!r}")
            del result[path]
        else:
            result[path] = (b"100644", str(arguments["contents"]).encode())
        return result

    def _edit(self, tree: dict[bytes, tuple[bytes, bytes]], step: int, allowed: list[str] | None = None,
              forced_path: str | None = None, forced_action: str | None = None) -> dict[str, object]:
        choices = [path for path in PATHS if allowed is None or under_prefix(path.encode(), allowed)]
        path = forced_path or choices[self.random.randrange(len(choices))]
        exists = path.encode() in tree
        actions = (["create"] if not exists else ["modify", "delete"])
        action = forced_action or actions[self.random.randrange(len(actions))]
        if action == "create" and exists:
            action = "modify"
        if action == "delete" and not exists:
            action = "create"
        number = self.edit_number
        self.edit_number += 1
        return {"path": path, "action": action, "contents": f"seed={self.seed} step={step} edit={number}\n"}

    def _write_edit(self, checkout: Path, arguments: dict[str, object]) -> None:
        target = checkout / str(arguments["path"])
        if arguments["action"] == "delete":
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(str(arguments["contents"]).encode())

    def _worker(self, now: int | None = None) -> bool:
        now = self.logical_now if now is None else now
        token = f"{self.seed:08x}{now:032x}"[-32:]
        with patch("worker.time.time", return_value=now), patch("worker.uuid.uuid4", return_value=SimpleNamespace(hex=token)):
            return worker.run_projection_job(self.root, now=now)

    def _stabilize(self) -> None:
        with locked(self.root):
            pass

    def rejection_snapshot(self) -> tuple[bytes, dict[str, str], str, str]:
        state = load_state(self.root)
        refs = {name: refs["refs/heads/main"] for name, refs in self._refs().items()}
        return ((self.root / "state.json").read_bytes(), refs,
                json.dumps(state["changes"], sort_keys=True),
                json.dumps({key: value.get("mappings") for key, value in state["transactions"].items()}, sort_keys=True))

    def assert_rejection_unchanged(self, before: tuple[bytes, dict[str, str], str, str]) -> None:
        if before != self.rejection_snapshot():
            raise AssertionError("rejection changed authoritative forge state")

    def checkpoint(self) -> None:
        check_invariants(self.root)
        state = load_state(self.root)
        canonical_oid = self._refs()["canonical"]["refs/heads/main"]
        if tree_at(forge.canonical_repo(self.root), canonical_oid) != self.canonical_tree:
            raise AssertionError("canonical tree differs from independent model")
        if canonical_oid not in self.trees:
            raise AssertionError("canonical model lacks published tip")
        for name, descriptor in self.views.items():
            view = state["views"][name]
            frontier = view["canonical_frontier"]
            expected = filtered(self.trees[frontier], descriptor["read"])  # type: ignore[arg-type]
            actual_oid = self._refs()[name]["refs/heads/main"]
            if actual_oid != view["projected_tip"] or tree_at(forge.view_repo(self.root, name), actual_oid) != expected:
                raise AssertionError(f"projected tree differs for {name}")
        for name, client in self.clients.items():
            actual_oid = git(client["path"], "rev-parse", "HEAD").stdout.decode().strip()  # type: ignore[arg-type]
            if actual_oid != client["oid"] or tree_at(client["path"], actual_oid) != client["tree"]:  # type: ignore[arg-type]
                raise AssertionError(f"client model differs for {name}")

    def _source_mapping_trees(self, old: str, new: str, snapshots: list[dict[bytes, tuple[bytes, bytes]]]) -> None:
        state = load_state(self.root)
        transaction = next(tx for tx in state["transactions"].values() if tx.get("projected_old") == old and tx.get("projected_new") == new)
        for mapping, snapshot in zip(transaction["mappings"], snapshots):
            self.trees[mapping["canonical"]] = snapshot
            if tree_at(forge.canonical_repo(self.root), mapping["canonical"]) != snapshot:
                raise AssertionError("accepted canonical transition differs from model")
        self.canonical_tree = snapshots[-1]

    def execute(self, operation: Operation, step: int) -> None:
        kind, arguments = operation.kind, operation.arguments
        if kind == "recover_dispatch":
            self._stabilize()
        elif kind == "admin_edit":
            old = git(self.admin, "rev-parse", "HEAD").stdout.decode().strip()
            canonical_old = self._refs()["canonical"]["refs/heads/main"]
            if old != canonical_old:
                git(self.admin, "fetch", "-q", "origin")
                git(self.admin, "reset", "-q", "--hard", "origin/main")
            self._write_edit(self.admin, arguments)
            new = self._commit(self.admin, f"admin step {step}", step)
            git(self.admin, "push", "-q", "origin", f"{new}:refs/staging/state-machine-{self.seed}-{step}")
            identity_harness.update_canonical(self.root, canonical_old, new)
            self.canonical_tree = self._apply(self.canonical_tree, arguments)
            self.trees[new] = dict(self.canonical_tree)
        elif kind == "client_refresh":
            name = str(arguments["view"]); client = self.clients[name]
            git(client["path"], "fetch", "-q", "origin")  # type: ignore[arg-type]
            git(client["path"], "reset", "-q", "--hard", "origin/main")  # type: ignore[arg-type]
            oid = git(client["path"], "rev-parse", "HEAD").stdout.decode().strip()  # type: ignore[arg-type]
            client.update(oid=oid, tree=tree_at(client["path"], oid), unpushed=False, clean=True)  # type: ignore[arg-type]
        elif kind in {"authorized_push", "unauthorized_push", "stale_push"}:
            name = str(arguments["view"]); client = self.clients[name]; checkout = client["path"]  # type: ignore[assignment]
            old = str(client["oid"]); snapshots = []; canonical_snapshots = []
            expected_canonical = dict(self.canonical_tree)
            for index, edit in enumerate(arguments["edits"]):  # type: ignore[union-attr]
                self._write_edit(checkout, edit)
                message = f"{kind} step {step} commit {index}"
                if edit.get("change_id"):  # type: ignore[union-attr]
                    message += f"\n\nChange-Id: {edit['change_id']}"  # type: ignore[index]
                oid = self._commit(checkout, message, step, index)
                client["tree"] = self._apply(client["tree"], edit)  # type: ignore[arg-type]
                client["oid"] = oid; client["unpushed"] = True; client["clean"] = True
                snapshots.append(dict(client["tree"]))  # type: ignore[arg-type]
                expected_canonical = self._apply(expected_canonical, edit)
                canonical_snapshots.append(dict(expected_canonical))
            before = self.rejection_snapshot() if kind != "authorized_push" else None
            pushed = git(checkout, "push", "-q", "origin", "main", check=False)
            if kind == "authorized_push":
                if pushed.returncode:
                    raise AssertionError(pushed.stderr.decode())
                self._source_mapping_trees(old, str(client["oid"]), canonical_snapshots)
                client["unpushed"] = False
            else:
                if not pushed.returncode:
                    raise AssertionError(f"{kind} unexpectedly succeeded")
                self.assert_rejection_unchanged(before)  # type: ignore[arg-type]
                if kind == "unauthorized_push":
                    git(checkout, "fetch", "-q", "origin")
                    git(checkout, "reset", "-q", "--hard", "origin/main")
                    oid = git(checkout, "rev-parse", "HEAD").stdout.decode().strip()
                    client.update(oid=oid, tree=tree_at(checkout, oid), unpushed=False, clean=True)
        elif kind == "add_view":
            name = str(arguments["name"]); read = list(arguments["read"]); write = list(arguments["write"])
            identity_harness.add_view(self.root, name, read, write, "main")
            self.views[name] = {"read": read, "write": write}
            self.addable = [descriptor for descriptor in self.addable if descriptor[0] != name]
            if write:
                self._clone_client(name)
        elif kind == "server_sync":
            name = str(arguments["view"]); before = self.rejection_snapshot() if arguments.get("blocked") else None
            try:
                forge.sync_view(self.root, name)
            except ForgeError:
                if not arguments.get("blocked"):
                    raise
                self.assert_rejection_unchanged(before)  # type: ignore[arg-type]
            else:
                if arguments.get("blocked"):
                    raise AssertionError("blocked sync unexpectedly succeeded")
        elif kind == "worker_complete":
            if not self._worker():
                raise AssertionError("expected claimable worker job")
        elif kind == "worker_fail_then_retry":
            claimed = worker.claim_projection_job(self.root, self.logical_now)
            if not claimed:
                raise AssertionError("expected claimable retry job")
            with patch("worker.time.time", return_value=self.logical_now):
                worker.fail_projection_job(self.root, claimed, RuntimeError("generated temporary failure"))
            self.checkpoint()
            self.logical_now += worker.RETRY_SECONDS + 1
            if not self._worker():
                raise AssertionError("retry was not claimed")
        elif kind in {"worker_crash_before_ref", "worker_crash_after_ref_and_recover"}:
            claimed = worker.claim_projection_job(self.root, self.logical_now)
            if not claimed:
                raise AssertionError("expected claimable crash job")
            plan = build_projection_plan(self.root, claimed["view"], claimed["view_state"], claimed["descriptor"], claimed["canonical_to"])
            with locked(self.root, recoverer=lambda _root, _state: None) as state:
                job = state["projection_jobs"][claimed["id"]]
                job["status"] = "ready"; job["plan"] = plan
                save_state(self.root, state)
            if kind == "worker_crash_before_ref":
                self.worker_crash = claimed["id"]
            else:
                forge.git(forge.view_repo(self.root, claimed["view"]), "update-ref", "refs/heads/main", plan["new_tip"], plan["old_tip"])
                self.logical_now += worker.LEASE_SECONDS + 1
                if not self._worker():
                    raise AssertionError("published plan was not adopted")
        elif kind == "recover_expired_worker":
            if not self.worker_crash:
                raise AssertionError("no worker crash to recover")
            state = load_state(self.root); expiry = state["projection_jobs"][self.worker_crash]["lease_expires_at"]
            self.logical_now = max(self.logical_now, int(expiry) + 1)
            if not self._worker():
                raise AssertionError("expired job was not recovered")
            self.worker_crash = None
        elif kind == "source_crash":
            name = str(arguments["view"]); client = self.clients[name]; checkout = client["path"]  # type: ignore[assignment]
            old = str(client["oid"]); edit = arguments["edit"]  # type: ignore[assignment]
            self._write_edit(checkout, edit); new = self._commit(checkout, f"source crash {step}", step)
            client["tree"] = self._apply(client["tree"], edit)  # type: ignore[arg-type]
            client["oid"] = new; client["unpushed"] = True
            forge.run("git", "--git-dir", str(forge.view_repo(self.root, name)), "fetch", "-q", str(checkout), new)
            update = f"{old} {new} refs/heads/main\n".encode()
            with patch.dict(os.environ, self.environment, clear=True):
                forge.pre_receive(self.root, name, update)
            state = load_state(self.root)
            tx = next(tx for tx in state["transactions"].values() if tx.get("projected_old") == old and tx.get("projected_new") == new)
            (self.root / "receivers" / tx["id"]).unlink(missing_ok=True)
            self._stabilize()
            self._source_mapping_trees(old, new, [self._apply(self.canonical_tree, edit)])
            self.source_crash = {"view": name, "old": old, "new": new, "probed": False}
        elif kind == "source_barrier_probes":
            if not self.source_crash:
                raise AssertionError("no source crash to probe")
            name = next(candidate for candidate in self.clients if candidate != self.source_crash["view"] and self.views[candidate]["write"])
            client = self.clients[name]; checkout = client["path"]  # type: ignore[assignment]
            # The probe must reach the forge's source barrier rather than Git's
            # local non-fast-forward check, so make this independent client current.
            git(checkout, "fetch", "-q", "origin"); git(checkout, "reset", "-q", "--hard", "origin/main")
            oid = git(checkout, "rev-parse", "HEAD").stdout.decode().strip()
            client.update(oid=oid, tree=tree_at(checkout, oid), unpushed=False, clean=True)
            edit = arguments["edit"]; self._write_edit(checkout, edit)
            self._commit(checkout, f"barrier probe {step}", step)
            before = self.rejection_snapshot(); rejected = git(checkout, "push", "-q", "origin", "main", check=False)
            if not rejected.returncode:
                raise AssertionError("barrier push unexpectedly succeeded")
            self.assert_rejection_unchanged(before)
            before = self.rejection_snapshot()
            if self._worker():
                raise AssertionError("worker crossed source barrier")
            self.assert_rejection_unchanged(before)
            before = self.rejection_snapshot()
            try:
                forge.sync_view(self.root, name)
            except ForgeError:
                self.assert_rejection_unchanged(before)
            else:
                raise AssertionError("barrier sync unexpectedly succeeded")
            git(checkout, "fetch", "-q", "origin"); git(checkout, "reset", "-q", "--hard", "origin/main")
            oid = git(checkout, "rev-parse", "HEAD").stdout.decode().strip()
            client.update(oid=oid, tree=tree_at(checkout, oid), unpushed=False, clean=True)
            self.source_crash["probed"] = True
        elif kind == "source_retry":
            if not self.source_crash or not self.source_crash["probed"]:
                raise AssertionError("source retry requires probes")
            name, old, new = (str(self.source_crash[key]) for key in ("view", "old", "new"))
            update = f"{old} {new} refs/heads/main\n".encode()
            with patch.dict(os.environ, self.environment, clear=True):
                forge.pre_receive(self.root, name, update)
                forge.git(forge.view_repo(self.root, name), "update-ref", "refs/heads/main", new, old)
                with patch("worker.time.time", return_value=self.logical_now), patch("worker.uuid.uuid4", return_value=SimpleNamespace(hex=f"{self.seed:032x}"[-32:])):
                    forge.post_receive(self.root, name, update)
            client = self.clients[name]; client["unpushed"] = False; client["clean"] = True
            self.source_crash = None
        else:
            raise AssertionError(f"unknown operation {kind}")
        self.checkpoint()

    def choose(self, step: int) -> Operation:
        # This short schedule ensures the bounded profile exercises durable
        # boundaries; later steps use weighted eligible ordinary operations.
        schedule = (
            "admin_hidden", "worker_complete", "worker_complete", "admin_visible",
            "worker_fail_then_retry", "worker_complete", "add_view", "refresh_alice",
            "authorized_push", "stale_push", "unauthorized_push", "server_sync",
            "admin_visible", "server_sync_blocked", "worker_crash_before_ref",
            "recover_expired_worker", "admin_visible",
            "worker_crash_after_ref_and_recover", "worker_complete", "worker_complete", "refresh_alice",
            "source_crash", "source_barrier_probes", "source_retry",
        )
        item = schedule[step] if step < len(schedule) else "random"
        if item == "admin_hidden":
            return Operation("admin_edit", self._edit(self.canonical_tree, step, forced_path="hidden/a.txt", forced_action="create"))
        if item == "admin_visible":
            if step < 16:
                return Operation("admin_edit", self._edit(self.canonical_tree, step, forced_path="shared/a.txt", forced_action="modify" if step > 3 else "create"))
            return Operation("admin_edit", self._edit(self.canonical_tree, step, forced_path="shared/a.txt", forced_action="delete"))
        if item == "worker_complete": return Operation("worker_complete", {})
        if item == "worker_fail_then_retry": return Operation("worker_fail_then_retry", {})
        if item == "refresh_alice": return Operation("client_refresh", {"view": "alice"})
        if item == "add_view":
            name, read, write = self.addable[0]; return Operation("add_view", {"name": name, "read": read, "write": write})
        if item == "authorized_push":
            first = self._edit(self.clients["alice"]["tree"], step, ["shared", "team-a"], forced_path="shared/base.txt", forced_action="modify")  # type: ignore[arg-type]
            first["change_id"] = self._change_id(step, 0)
            second = self._edit(self.clients["alice"]["tree"], step, ["shared", "team-a"], forced_path="team-a/a.txt", forced_action="create")  # type: ignore[arg-type]
            return Operation("authorized_push", {"view": "alice", "edits": [first, second]})
        if item == "stale_push":
            edit = self._edit(self.clients["bob"]["tree"], step, ["shared", "team-b"], forced_path="shared/b.txt", forced_action="create")  # type: ignore[arg-type]
            return Operation("stale_push", {"view": "bob", "edits": [edit]})
        if item == "client_refresh": return Operation("client_refresh", {"view": "bob"})
        if item == "unauthorized_push":
            edit = self._edit(self.clients["alice"]["tree"], step, forced_path="hidden/a.txt", forced_action="create")  # type: ignore[arg-type]
            return Operation("unauthorized_push", {"view": "alice", "edits": [edit]})
        if item == "server_sync": return Operation("server_sync", {"view": "alice", "blocked": False})
        if item == "server_sync_blocked": return Operation("server_sync", {"view": "alice", "blocked": True})
        if item == "worker_crash_before_ref": return Operation(item, {})
        if item == "recover_expired_worker": return Operation(item, {})
        if item == "worker_crash_after_ref_and_recover": return Operation(item, {})
        if item == "source_crash":
            # Alice is stale after the admin update, so refresh it as a traced
            # ordinary action before this source operation when necessary.
            if self.clients["alice"]["oid"] != self._refs()["alice"]["refs/heads/main"]:
                return Operation("client_refresh", {"view": "alice"})
            edit = self._edit(self.clients["alice"]["tree"], step, ["shared", "team-a"], forced_path="team-a/b.txt", forced_action="create")  # type: ignore[arg-type]
            return Operation("source_crash", {"view": "alice", "edit": edit})
        if item == "source_barrier_probes":
            edit = self._edit(self.clients["bob"]["tree"], step, ["shared", "team-b"], forced_path="shared/a.txt", forced_action="modify")  # type: ignore[arg-type]
            return Operation("source_barrier_probes", {"edit": edit})
        if item == "source_retry": return Operation("source_retry", {})
        # Bounded random tail only selects operations whose preconditions are
        # straightforward and currently true.
        state = load_state(self.root)
        syncable = [
            name for name in self.views
            if not any(job["view"] == name and job["status"] != "completed" for job in state["projection_jobs"].values())
        ]
        candidates = ["client_refresh"]
        if syncable and not self.source_crash:
            candidates.append("server_sync")
        claimable = any(
            job["status"] in {"pending", "retry"} and job.get("next_attempt_at", 0) <= self.logical_now
            for job in state["projection_jobs"].values()
        )
        if not self.source_crash and not self.worker_crash:
            candidates.append("admin_edit")
            if claimable:
                candidates.append("worker_complete")
        choice = self.random.choice(candidates)
        if choice == "client_refresh": return Operation(choice, {"view": self.random.choice(list(self.clients))})
        if choice == "server_sync": return Operation(choice, {"view": self.random.choice(syncable), "blocked": False})
        if choice == "worker_complete": return Operation(choice, {})
        return Operation(choice, self._edit(self.canonical_tree, step))

    def settle(self) -> None:
        if self.source_crash:
            if not self.source_crash["probed"]:
                edit = self._edit(self.clients["bob"]["tree"], self.steps, ["shared", "team-b"])  # type: ignore[arg-type]
                self.execute(Operation("source_barrier_probes", {"edit": edit, "phase": "epilogue"}), self.steps)
            self.execute(Operation("source_retry", {"phase": "epilogue"}), self.steps)
        if self.worker_crash:
            self.execute(Operation("recover_expired_worker", {"phase": "epilogue"}), self.steps)
        while True:
            state = load_state(self.root)
            incomplete = [job for job in state["projection_jobs"].values() if job["status"] != "completed"]
            if not incomplete:
                break
            self.logical_now += worker.RETRY_SECONDS + 1
            self.execute(Operation("worker_complete", {"phase": "epilogue"}), self.steps)


def _format_failure(seed: int, steps: int, failed_step: int, operation: Operation, trace: list[Operation], error: Exception) -> AssertionError:
    lines = ["state-machine failure", f"seed: {seed}", f"steps: {steps}", f"failed step: {failed_step}",
             f"operation: {operation.kind} {json.dumps(operation.arguments, sort_keys=True)}", "trace:"]
    lines.extend(f"{index:04d} {entry.kind} {json.dumps(entry.arguments, sort_keys=True)}" for index, entry in enumerate(trace))
    lines.append(str(error))
    return AssertionError("\n".join(lines))


def _run(seed: int, steps: int, operations: list[Operation] | None) -> RunResult:
    with tempfile.TemporaryDirectory() as temporary:
        scenario = Scenario(seed, steps, Path(temporary))
        position = 0
        try:
            if operations is None:
                for step in range(steps):
                    automatic = Operation("recover_dispatch", {"before_step": step})
                    scenario.trace.append(automatic); scenario.execute(automatic, step)
                    selected = scenario.choose(step)
                    scenario.trace.append(selected); scenario.execute(selected, step)
                scenario.settle()
            else:
                current_step = 0
                for operation in operations:
                    scenario.trace.append(operation)
                    if operation.kind == "recover_dispatch":
                        current_step = int(operation.arguments["before_step"])
                    scenario.execute(operation, current_step)
                    position += 1
            state = load_state(scenario.root)
            if any(job["status"] != "completed" for job in state["projection_jobs"].values()):
                raise AssertionError("scenario did not settle all projection jobs")
            return RunResult(seed, steps, list(scenario.trace), (scenario.root / "state.json").read_bytes(), scenario._refs())
        except (AssertionError, ForgeError, KeyError, StopIteration) as error:
            operation = scenario.trace[-1] if scenario.trace else Operation("fixture", {})
            raise _format_failure(seed, steps, position if operations is not None else min(steps, len(scenario.trace) // 2), operation, scenario.trace, error) from error


def run_scenario(seed: int, steps: int) -> RunResult:
    """Create a fresh forge, execute one deterministic sequence, and verify it."""
    if steps <= 0:
        raise AssertionError("state-machine steps must be positive")
    return _run(seed, steps, None)


def replay_scenario(seed: int, operations: list[Operation]) -> RunResult:
    """Execute recorded operations directly without consulting random state."""
    if not operations:
        raise AssertionError("state-machine trace must not be empty")
    requested = max((int(item.arguments.get("before_step", -1)) for item in operations), default=-1) + 1
    return _run(seed, requested, operations)
