"""Git command and object-database adapter."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from errors import ForgeError
from layout import ZERO_OID
from validation import TreeEntries, has_file_directory_conflict


def run(
    *args: str | bytes,
    cwd: Path | None = None,
    data: bytes | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.run(
        args,
        cwd=cwd,
        input=data,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and process.returncode:
        detail = process.stderr.decode(errors="replace").strip()
        raise ForgeError(f"{' '.join(os.fsdecode(arg) for arg in args)}: {detail}")
    return process


def clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_GRAFT_FILE",
        "GIT_SHALLOW_FILE",
        "GIT_REPLACE_REF_BASE",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
        "GIT_INTERNAL_SUPER_PREFIX",
        "GIT_QUARANTINE_PATH",
    ):
        env.pop(name, None)
    for name in list(env):
        if name == "GIT_CONFIG" or name == "GIT_CONFIG_PARAMETERS" or name.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            env.pop(name, None)
    env.pop("GIT_CONFIG_COUNT", None)
    # Forge-created commits must not inherit encoding or other defaults from a
    # user's Git installation.  Repository-local configuration remains active.
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return env


def git(
    repo: Path,
    *args: str | bytes,
    data: bytes | None = None,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return run(
        "git",
        "--git-dir",
        os.fsencode(repo),
        *args,
        data=data,
        env=env or clean_env(),
        check=check,
    )


def output(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return git(repo, *args, env=env).stdout.decode().strip()


def ref(repo: Path, name: str, env: dict[str, str] | None = None) -> str:
    result = git(repo, "rev-parse", "--verify", name, env=env, check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else ZERO_OID


def has_exact_object_closure(repo: Path) -> bool:
    """Whether the object database contains exactly its reachable ref closure."""
    stored = git(
        repo,
        "cat-file",
        "--batch-all-objects",
        "--batch-check=%(objectname)",
        check=False,
    )
    reachable = git(
        repo,
        "rev-list",
        "--objects",
        "--no-object-names",
        "--all",
        check=False,
    )
    if stored.returncode or reachable.returncode:
        return False
    return set(stored.stdout.splitlines()) == set(reachable.stdout.splitlines())


def tree_entries(
    repo: Path, treeish: str, env: dict[str, str] | None = None
) -> TreeEntries:
    raw = git(repo, "ls-tree", "-rz", "--full-tree", treeish, env=env).stdout
    entries: TreeEntries = {}
    for item in raw.split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        mode, kind, oid = metadata.split(b" ")
        entries[path] = (mode, kind, oid.decode())
    return entries


def validate_no_empty_subtrees(
    repo: Path, treeish: str, env: dict[str, str] | None = None
) -> None:
    raw = git(repo, "ls-tree", "-rzt", "--full-tree", treeish, env=env).stdout
    trees: list[bytes] = []
    leaves: list[bytes] = []
    for item in raw.split(b"\0"):
        if not item:
            continue
        metadata, path = item.split(b"\t", 1)
        _mode, kind, _oid = metadata.split(b" ")
        (trees if kind == b"tree" else leaves).append(path)
    for tree in trees:
        if not any(path.startswith(tree + b"/") for path in leaves):
            raise ForgeError("explicit empty subtrees are not supported")


def tree_oid(repo: Path, commit: str, env: dict[str, str] | None = None) -> str:
    return output(repo, "rev-parse", f"{commit}^{{tree}}", env=env)


def copy_object(
    source: Path,
    destination: Path,
    oid: str,
    source_env: dict[str, str] | None = None,
    destination_env: dict[str, str] | None = None,
) -> None:
    kind = output(source, "cat-file", "-t", oid, env=source_env)
    contents = git(source, "cat-file", kind, oid, env=source_env).stdout
    copied = git(
        destination,
        "hash-object",
        "-w",
        "-t",
        kind,
        "--stdin",
        data=contents,
        env=destination_env or clean_env(),
    ).stdout.decode().strip()
    if copied != oid:
        raise ForgeError("object format mismatch while copying object")


def temporary_index(
    repo: Path, object_env: dict[str, str] | None = None
) -> tuple[dict[str, str], Path]:
    descriptor, raw = tempfile.mkstemp(prefix="index-", dir=repo.parent)
    os.close(descriptor)
    path = Path(raw)
    path.unlink()
    env = (object_env or clean_env()).copy()
    env["GIT_INDEX_FILE"] = str(path)
    return env, path


def write_tree(
    repo: Path,
    entries: TreeEntries,
    object_env: dict[str, str] | None = None,
) -> str:
    if has_file_directory_conflict(entries):
        raise ForgeError("tree transition has a file/directory conflict")
    env, index_path = temporary_index(repo, object_env)
    try:
        git(repo, "read-tree", "--empty", env=env)
        records = [
            mode + b" " + oid.encode() + b"\t" + path + b"\0"
            for path, (mode, _kind, oid) in sorted(entries.items())
        ]
        if records:
            git(repo, "update-index", "-z", "--index-info", data=b"".join(records), env=env)
        else:
            # ``write-tree`` may return Git's well-known empty-tree ID without
            # storing it, leaving an otherwise valid empty projected view unreadable.
            return git(repo, "hash-object", "-w", "-t", "tree", "--stdin", data=b"", env=env).stdout.decode().strip()
        return output(repo, "write-tree", env=env)
    finally:
        index_path.unlink(missing_ok=True)


def commit_message(repo: Path, oid: str, env: dict[str, str] | None = None) -> bytes:
    raw = git(repo, "cat-file", "commit", oid, env=env).stdout
    return raw.split(b"\n\n", 1)[1] if b"\n\n" in raw else b""


def commit_metadata(
    repo: Path, oid: str, env: dict[str, str] | None = None
) -> tuple[bytes, int, int]:
    """Return the author email and author/committer epoch timestamps."""
    values = (
        git(repo, "show", "-s", "--format=%ae%x00%at%x00%ct", oid, env=env)
        .stdout.rstrip(b"\n")
        .split(b"\0")
    )
    if len(values) != 3:
        raise ForgeError("unsupported commit metadata")
    email = values[0].strip().lower()
    try:
        author_ts = int(values[1])
        committer_ts = int(values[2])
    except ValueError as error:
        raise ForgeError("malformed commit timestamps") from error
    return email, author_ts, committer_ts


def author_environment(
    repo: Path,
    oid: str,
    env: dict[str, str] | None = None,
    projected: bool = False,
) -> dict[str, str]:
    if projected:
        return {
            "GIT_AUTHOR_NAME": "Bastion Projection",
            "GIT_AUTHOR_EMAIL": "projection@localhost",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00 +0000",
            "GIT_COMMITTER_NAME": "Bastion Projection",
            "GIT_COMMITTER_EMAIL": "projection@localhost",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00 +0000",
        }
    values = (
        git(repo, "show", "-s", "--format=%an%x00%ae%x00%aI", oid, env=env)
        .stdout.rstrip(b"\n")
        .split(b"\0")
    )
    if len(values) != 3:
        raise ForgeError("unsupported commit author metadata")
    name, email, date = (
        value.decode(errors="surrogateescape") for value in values
    )
    return {
        "GIT_AUTHOR_NAME": name,
        "GIT_AUTHOR_EMAIL": email,
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_NAME": "Bastion Forge",
        "GIT_COMMITTER_EMAIL": "forge@localhost",
        "GIT_COMMITTER_DATE": date,
    }


def make_commit(
    repo: Path,
    tree: str,
    parent: str,
    message: bytes,
    metadata: dict[str, str],
    object_env: dict[str, str] | None = None,
) -> str:
    args: list[str] = ["commit-tree", tree]
    if parent != ZERO_OID:
        args.extend(("-p", parent))
    env = (object_env or clean_env()) | metadata
    return git(repo, *args, data=message, env=env).stdout.decode().strip()


def first_parent_commits(repo: Path, start: str, end: str) -> list[str]:
    revision = end if start == ZERO_OID else f"{start}..{end}"
    lines = output(repo, "rev-list", "--reverse", "--first-parent", revision)
    commits = lines.splitlines() if lines else []
    for oid in commits:
        parents = output(repo, "show", "-s", "--format=%P", oid).split()
        if len(parents) > 1:
            raise ForgeError(
                "canonical merge commits are not supported by this experiment"
            )
    return commits


def submitted_commits(
    repo: Path, old: str, new: str, env: dict[str, str]
) -> list[str]:
    if git(
        repo, "merge-base", "--is-ancestor", old, new, env=env, check=False
    ).returncode:
        raise ForgeError("force updates are not supported")
    commits = output(repo, "rev-list", "--reverse", f"{old}..{new}", env=env).splitlines()
    if not commits:
        raise ForgeError("push contains no new commits")
    expected_parent = old
    for oid in commits:
        parents = output(repo, "show", "-s", "--format=%P", oid, env=env).split()
        if parents != [expected_parent]:
            raise ForgeError(
                "submitted history must be a linear chain from the advertised tip"
            )
        expected_parent = oid
    return commits


def quarantine_objects(environ: dict[str, str] | None = None) -> set[str]:
    raw = (environ or os.environ).get("GIT_QUARANTINE_PATH")
    if not raw:
        return set()
    quarantine = Path(raw)
    objects: set[str] = set()
    for directory in quarantine.iterdir():
        if len(directory.name) == 2 and all(
            char in "0123456789abcdef" for char in directory.name
        ):
            for item in directory.iterdir():
                if len(item.name) == 38:
                    objects.add(directory.name + item.name)
    pack = quarantine / "pack"
    for index in pack.glob("*.idx") if pack.exists() else ():
        listing = run("git", "verify-pack", "-v", str(index)).stdout.decode(
            errors="replace"
        )
        for line in listing.splitlines():
            fields = line.split()
            if (
                len(fields) >= 2
                and len(fields[0]) == 40
                and fields[1] in {"commit", "tree", "blob", "tag"}
            ):
                objects.add(fields[0])
    return objects


def verify_no_extra_objects(repo: Path, new: str, env: dict[str, str]) -> None:
    quarantined = quarantine_objects(env)
    if not quarantined:
        return
    reachable = set(
        output(repo, "rev-list", "--objects", "--no-object-names", new, env=env).splitlines()
    )
    if quarantined - reachable:
        raise ForgeError("push contains objects unreachable from the submitted tip")


def copy_submitted_closure(repo: Path, old: str, new: str, env: dict[str, str]) -> None:
    object_ids = output(
        repo, "rev-list", "--objects", "--no-object-names", new, f"^{old}", env=env
    ).splitlines()
    for oid in object_ids:
        copy_object(repo, repo, oid, source_env=env)
