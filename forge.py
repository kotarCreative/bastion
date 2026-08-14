#!/usr/bin/env python3
"""CLI and compatibility facade for the Veil experimental Git forge."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from errors import ForgeError
from git import git, output, ref, run
from layout import ZERO_OID, canonical_repo, view_repo
from protocol import post_receive, pre_receive
from service import add_view as _add_view
from service import init_forge, sync_view, update_canonical_ref
from state import save_state
from worker import drain_projection_jobs


def add_view(
    root: Path, name: str, read: list[str], write: list[str], branch: str
) -> None:
    _add_view(root, name, read, write, branch, Path(__file__))


def new_change_id() -> str:
    return "I" + hashlib.sha1(os.urandom(32)).hexdigest()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("root", type=Path)
    init.add_argument("source", type=Path)
    init.add_argument("--branch", default="main")
    add = commands.add_parser("add-view")
    add.add_argument("root", type=Path)
    add.add_argument("name")
    add.add_argument("--read", action="append", required=True)
    add.add_argument("--write", action="append", default=[])
    add.add_argument("--branch", default="main")
    sync = commands.add_parser("sync")
    sync.add_argument("root", type=Path)
    sync.add_argument("name")
    commands.add_parser("new-change-id")
    work = commands.add_parser("work")
    work.add_argument("root", type=Path)
    work.add_argument("--limit", type=int)
    canonical = commands.add_parser("update-canonical")
    canonical.add_argument("root", type=Path)
    canonical.add_argument("expected")
    canonical.add_argument("new")
    pre = commands.add_parser("pre-receive")
    pre.add_argument("root", type=Path)
    pre.add_argument("name")
    post = commands.add_parser("post-receive")
    post.add_argument("root", type=Path)
    post.add_argument("name")
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "init":
            init_forge(args.root, args.source, args.branch)
        elif args.command == "add-view":
            add_view(args.root, args.name, args.read, args.write, args.branch)
        elif args.command == "sync":
            sync_view(args.root, args.name)
        elif args.command == "new-change-id":
            print(new_change_id())
        elif args.command == "work":
            drain_projection_jobs(args.root, args.limit)
        elif args.command == "update-canonical":
            update_canonical_ref(args.root, args.expected, args.new)
        elif args.command == "pre-receive":
            pre_receive(args.root, args.name, sys.stdin.buffer.read())
        elif args.command == "post-receive":
            post_receive(args.root, args.name, sys.stdin.buffer.read())
        return 0
    except ForgeError as error:
        print(f"veil: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
