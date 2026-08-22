

"""Binary Git plumbing and independent snapshot checks for tree-operation tests."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path

import forge
from tests import identity_harness
from tests.storage_helpers import document_bytes, read_document


Tree = dict[bytes, tuple[bytes, str, bytes]]
Effect = tuple[bytes, bytes] | None
Effects = dict[bytes, Effect]
ZERO_OID = "0" * 40
ALLOWED_MODES = {b"100644", b"100755", b"120000"}


def clean_environment() -> dict[str, str]:
    """Isolate plumbing from ambient Git configuration and repositories."""
    environment = dict(os.environ)
    names = {
        "GIT_DIR", "GIT_WORK_TREE", "GIT_IMPLICIT_WORK_TREE", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
        "GIT_GRAFT_FILE", "GIT_SHALLOW_FILE", "GIT_REPLACE_REF_BASE", "GIT_NAMESPACE",
        "GIT_PREFIX", "GIT_INTERNAL_SUPER_PREFIX", "GIT_QUARANTINE_PATH", "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT",
    }
    for name in list(environment):
        if name in names or name.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(name, None)
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1", "LC_ALL": "C", "LANG": "C",
    })
    return environment


def command(
    *args: str | bytes,
    cwd: Path | None = None,
    data: bytes | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(
        args, cwd=cwd, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=env or clean_environment(), check=False,
    )
    if check and result.returncode:
        rendered = " ".join(repr(os.fsdecode(argument)) for argument in args)
        raise AssertionError(f"Git command failed: {rendered}\n{result.stderr.decode(errors='replace')}")
    return result


def git(repo: Path, *args: str | bytes, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
    return command("git", "-C", os.fsdecode(repo), *args, **kwargs)  # type: ignore[arg-type]


def oid(repo: Path, name: str) -> str:
    result = git(repo, "rev-parse", "--verify", name, check=False)
    return result.stdout.decode("ascii").strip() if result.returncode == 0 else ZERO_OID


def tree_at(repo: Path, commit: str) -> Tree:
    """Read raw leaf paths, modes, object IDs, and blob bytes without revision paths."""
    tree: Tree = {}
    for record in git(repo, "ls-tree", "-rz", "--full-tree", commit).stdout.split(b"\0"):
        if not record:
            continue
        metadata, path = record.split(b"\t", 1)
        mode, kind, object_id = metadata.split(b" ")
        if kind != b"blob":
            raise AssertionError(f"unexpected non-blob leaf {path!r}")
        tree[path] = (mode, object_id.decode("ascii"), git(repo, "cat-file", "blob", object_id).stdout)
    return tree


def _metadata(timestamp: int) -> dict[str, str]:
    date = f"{timestamp} +0000"
    return clean_environment() | {
        "GIT_AUTHOR_NAME": "Tree Fixture", "GIT_AUTHOR_EMAIL": "tree@example.test",
        "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_NAME": "Tree Fixture",
        "GIT_COMMITTER_EMAIL": "tree@example.test", "GIT_COMMITTER_DATE": date,
    }


def build_commit(
    repo: Path,
    parent: str | None,
    effects: Effects,
    message: bytes,
    timestamp: int,
) -> str:
    """Build a valid leaf-only commit by temporary-index plumbing."""
    entries: dict[bytes, tuple[bytes, bytes]] = {}
    if parent:
        entries = {path: (mode, contents) for path, (mode, _oid, contents) in tree_at(repo, parent).items()}
    for path, effect in effects.items():
        if effect is None:
            entries.pop(path, None)
        else:
            mode, contents = effect
            if mode not in ALLOWED_MODES:
                raise AssertionError(f"unsupported normal fixture mode {mode!r}")
            entries[path] = effect
    descriptor, raw_index = tempfile.mkstemp(prefix="tree-index-", dir=repo)
    os.close(descriptor)
    index = Path(raw_index)
    index.unlink()
    environment = clean_environment() | {"GIT_INDEX_FILE": str(index)}
    try:
        git(repo, "read-tree", "--empty", env=environment)
        records: list[bytes] = []
        for path, (mode, contents) in sorted(entries.items()):
            object_id = git(repo, "hash-object", "-w", "--stdin", data=contents).stdout.strip()
            records.append(mode + b" " + object_id + b"\t" + path + b"\0")
        if records:
            git(repo, "update-index", "-z", "--index-info", data=b"".join(records), env=environment)
        tree = git(repo, "write-tree", env=environment).stdout.decode("ascii").strip()
        arguments: list[str] = ["commit-tree", tree]
        if parent:
            arguments.extend(("-p", parent))
        commit = git(repo, *arguments, data=message, env=_metadata(timestamp)).stdout.decode("ascii").strip()
        git(repo, "update-ref", "refs/heads/main", commit, parent or ZERO_OID)
        return commit
    finally:
        index.unlink(missing_ok=True)


def empty_subtree_commit(repo: Path, parent: str, timestamp: int) -> str:
    """Construct the narrow explicit-empty-subtree rejection fixture."""
    empty = git(repo, "mktree", "-z", data=b"").stdout.decode("ascii").strip()
    shared = git(repo, "mktree", "-z", data=b"040000 tree " + empty.encode() + b"\tempty\0").stdout.decode("ascii").strip()
    root = git(repo, "mktree", "-z", data=b"040000 tree " + shared.encode() + b"\tshared\0").stdout.decode("ascii").strip()
    commit = git(repo, "commit-tree", root, "-p", parent, data=b"empty subtree\n", env=_metadata(timestamp)).stdout.decode("ascii").strip()
    git(repo, "update-ref", "refs/heads/main", commit, parent)
    return commit


def gitlink_commit(repo: Path, parent: str, timestamp: int) -> str:
    """Construct the narrow gitlink rejection fixture using a real commit OID."""
    shared = git(repo, "mktree", "-z", data=b"160000 commit " + parent.encode() + b"\tmodule\0").stdout.decode("ascii").strip()
    root = git(repo, "mktree", "-z", data=b"040000 tree " + shared.encode() + b"\tshared\0").stdout.decode("ascii").strip()
    commit = git(repo, "commit-tree", root, "-p", parent, data=b"gitlink\n", env=_metadata(timestamp)).stdout.decode("ascii").strip()
    git(repo, "update-ref", "refs/heads/main", commit, parent)
    return commit


def allowed(path: bytes, prefixes: list[str]) -> bool:
    return any(not prefix or path == prefix.encode("ascii") or path.startswith(prefix.encode("ascii") + b"/") for prefix in prefixes)


def filtered(tree: Tree, prefixes: list[str]) -> Tree:
    return {path: entry for path, entry in tree.items() if allowed(path, prefixes)}


class TreeFixture:
    """A fresh multi-view forge with an independent byte-level snapshot model."""

    policies = {
        "alice": (["shared", "team-a"], ["shared", "team-a"]),
        "bob": (["shared", "team-b"], ["shared", "team-b"]),
        "limited": (["shared", "team-a"], ["shared"]),
    }

    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)
        self.root = self.directory / "forge"
        self.source = self.directory / "source"
        self.clients: dict[str, Path] = {}
        self.policies = dict(self.policies)
        self.counter = 0
        command("git", "init", "-q", "--object-format=sha1", "-b", "main", os.fsdecode(self.source))
        initial: Effects = {
            b"shared/base.txt": (b"100644", b"shared baseline\n"),
            b"shared/delete.txt": (b"100644", b"shared delete\n"),
            b"shared/rename.txt": (b"100644", b"shared rename\n"),
            b"shared/mode-up": (b"100644", b"mode up\n"),
            b"shared/mode-down": (b"100755", b"mode down\n"),
            b"shared/link": (b"120000", b"base.txt"),
            b"shared/node": (b"100644", b"node\n"),
            b"team-a/private.txt": (b"100644", b"alice private\n"),
            b"team-a/private-exec": (b"100755", b"private executable\n"),
            b"team-a/private-delete.txt": (b"100644", b"private delete\n"),
            b"team-b/private.txt": (b"100644", b"bob private\n"),
            b"hidden/secret.txt": (b"100644", b"unshared hidden secret bytes\n"),
        }
        initial_tip = build_commit(self.source, None, initial, b"initial fixture\n", 1_700_000_000)
        forge.init_forge(self.root, self.source, "main")
        self.admin_principal = identity_harness.provision_admin(self.root)
        for name, (read, write) in self.policies.items():
            identity_harness.add_view(self.root, name, read, write, "main")
            checkout = self.directory / f"client-{name}"
            command("git", "clone", "-q", "--no-local", os.fsdecode(forge.view_repo(self.root, name)), os.fsdecode(checkout))
            self.clients[name] = checkout
        self.admin = self.directory / "admin"
        command("git", "clone", "-q", "--no-local", os.fsdecode(forge.canonical_repo(self.root)), os.fsdecode(self.admin))
        self.models: dict[str, Tree] = {initial_tip: tree_at(self.source, initial_tip)}
        self.canonical_model = dict(self.models[initial_tip])
        self.hidden_oid = self.canonical_model[b"hidden/secret.txt"][1]
        self.checkpoint()

    def close(self) -> None:
        self.temporary.cleanup()

    def _next_timestamp(self) -> int:
        self.counter += 1
        return 1_700_000_000 + self.counter

    def _apply(self, model: Tree, effects: Effects) -> Tree:
        result = dict(model)
        for path, effect in effects.items():
            if effect is None:
                result.pop(path, None)
            else:
                mode, contents = effect
                object_id = git(self.source, "hash-object", "-w", "--stdin", data=contents).stdout.decode("ascii").strip()
                result[path] = (mode, object_id, contents)
        return result

    def source_commit(self, view: str, effects: Effects, message: bytes = b"source change\n") -> tuple[str, str]:
        client = self.clients[view]
        old = oid(client, "refs/heads/main")
        new = build_commit(client, old, effects, message, self._next_timestamp())
        return old, new

    def push_source(self, view: str, effects: Effects, message: bytes = b"source change\n") -> tuple[str, str]:
        old, new = self.source_commit(view, effects, message)
        command("git", "-C", os.fsdecode(self.clients[view]), "push", "origin", "main")
        forge.drain_projection_jobs(self.root)
        state = self.state()
        transaction = next(tx for tx in state["transactions"].values() if tx.get("view") == view and tx.get("projected_old") == old and tx.get("projected_new") == new)
        self.canonical_model = self._apply(self.canonical_model, effects)
        for mapping in transaction["mappings"]:
            self.models[mapping["canonical"]] = dict(self.canonical_model)
        self.checkpoint()
        return old, new

    def push_source_chain(self, view: str, chain: list[Effects]) -> tuple[str, str]:
        client = self.clients[view]
        old = oid(client, "refs/heads/main")
        parent = old
        for index, effects in enumerate(chain):
            parent = build_commit(client, parent, effects, f"source chain {index}\n".encode(), self._next_timestamp())
        command("git", "-C", os.fsdecode(client), "push", "origin", "main")
        forge.drain_projection_jobs(self.root)
        transaction = next(tx for tx in self.state()["transactions"].values() if tx.get("view") == view and tx.get("projected_old") == old and tx.get("projected_new") == parent)
        for effects, mapping in zip(chain, transaction["mappings"]):
            self.canonical_model = self._apply(self.canonical_model, effects)
            self.models[mapping["canonical"]] = dict(self.canonical_model)
        self.checkpoint()
        return old, parent

    def admin_update(self, chain: list[Effects]) -> list[str]:
        canonical = forge.canonical_repo(self.root)
        expected = oid(canonical, "refs/heads/main")
        git(self.admin, "update-ref", "refs/heads/main", expected)
        parent = expected
        commits: list[str] = []
        for index, effects in enumerate(chain):
            parent = build_commit(self.admin, parent, effects, f"admin change {index}\n".encode(), self._next_timestamp())
            commits.append(parent)
        staging = f"refs/staging/tree-{self.counter}"
        command("git", "-C", os.fsdecode(self.admin), "push", "origin", f"{parent}:{staging}")
        identity_harness.update_canonical(self.root, expected, parent, self.admin_principal)
        forge.drain_projection_jobs(self.root)
        for commit, effects in zip(commits, chain):
            self.canonical_model = self._apply(self.canonical_model, effects)
            self.models[commit] = dict(self.canonical_model)
        self.checkpoint()
        return commits

    def state(self) -> dict:
        return read_document(self.root)

    def refs(self) -> dict[str, tuple[tuple[str, str, str], ...]]:
        repositories = {"canonical": forge.canonical_repo(self.root)} | {name: forge.view_repo(self.root, name) for name in self.policies}
        result: dict[str, tuple[tuple[str, str, str], ...]] = {}
        for name, repository in repositories.items():
            lines = git(repository, "for-each-ref", "--format=%(refname) %(objectname) %(objecttype)").stdout.decode("ascii").splitlines()
            result[name] = tuple(tuple(line.split(" ", 2)) for line in lines)
        return result

    def rejection_snapshot(self) -> tuple[bytes, dict[str, tuple[tuple[str, str, str], ...]], tuple[str, ...]]:
        markers = self.root / "receivers"
        return (document_bytes(self.root), self.refs(), tuple(sorted(path.name for path in markers.iterdir())) if markers.exists() else ())

    def assert_rejection(self, before: tuple[bytes, dict[str, tuple[tuple[str, str, str], ...]], tuple[str, ...]]) -> None:
        if before != self.rejection_snapshot():
            raise AssertionError("rejection changed authoritative state or refs")
        forge.check_invariants(self.root)

    def checkpoint(self) -> None:
        forge.check_invariants(self.root)
        state = self.state()
        canonical = forge.canonical_repo(self.root)
        if tree_at(canonical, oid(canonical, "refs/heads/main")) != self.canonical_model:
            raise AssertionError("canonical tree differs from independent model")
        for name, (read, _write) in self.policies.items():
            view = state["views"][name]
            expected = filtered(self.models[view["canonical_frontier"]], read)
            repository = forge.view_repo(self.root, name)
            actual = oid(repository, view["projected_ref"])
            if actual != view["projected_tip"] or tree_at(repository, actual) != expected:
                raise AssertionError(f"projected tree differs for {name}")
            if git(repository, "cat-file", "-e", self.hidden_oid, check=False).returncode == 0:
                raise AssertionError(f"hidden blob is present in {name}")
