#!/usr/bin/env python3
"""CLI and compatibility facade for the Bastion experimental Git forge."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from errors import ForgeError
from invariants import check_invariants
from git import git, output, ref, run
from layout import ZERO_OID, canonical_repo, view_repo
from protocol import post_receive, pre_receive
from service import (
    add_principal as _add_principal,
    add_role as _add_role,
    add_service_account as _add_service_account,
    add_team as _add_team,
    add_view as _add_view,
    bind_role as _bind_role,
    init_forge,
    sync_view,
    transition_principal,
    update_canonical_ref,
)
from state import save_state
from worker import drain_projection_jobs


def add_view(
    root: Path, name: str, principal: str, branch: str
) -> None:
    _add_view(root, name, principal, branch, Path(__file__))


def new_change_id() -> str:
    return "I" + hashlib.sha1(os.urandom(32)).hexdigest()


def capability_grants(pairs: list[str]) -> dict[str, list[str]]:
    from identity import CAPABILITIES

    grants = {capability: [] for capability in CAPABILITIES}
    for pair in pairs:
        capability, separator, path = pair.partition("=")
        if not separator or capability not in CAPABILITIES:
            raise SystemExit(
                f"grant must be CAPABILITY=PATH with one of: {', '.join(CAPABILITIES)}"
            )
        grants[capability].append(path)
    return grants


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("root", type=Path)
    init.add_argument("source", type=Path)
    init.add_argument("--branch", default="main")
    principal = commands.add_parser("add-principal")
    principal.add_argument("root", type=Path)
    principal.add_argument("principal")
    principal.add_argument("--email", action="append", default=[])
    service = commands.add_parser("add-service-account")
    service.add_argument("root", type=Path)
    service.add_argument("service")
    service.add_argument("--owner", required=True)
    service.add_argument("--email", action="append", default=[])
    team = commands.add_parser("add-team")
    team.add_argument("root", type=Path)
    team.add_argument("team")
    team.add_argument("--member", action="append", default=[])
    role = commands.add_parser("add-role")
    role.add_argument("root", type=Path)
    role.add_argument("role")
    role.add_argument("--allow", action="append", default=[], metavar="CAPABILITY=PATH")
    role.add_argument("--deny", action="append", default=[], metavar="CAPABILITY=PATH")
    bind = commands.add_parser("bind-role")
    bind.add_argument("root", type=Path)
    bind.add_argument("role")
    subjects = bind.add_mutually_exclusive_group(required=True)
    subjects.add_argument("--principal")
    subjects.add_argument("--team")
    add = commands.add_parser("add-view")
    add.add_argument("root", type=Path)
    add.add_argument("name")
    add.add_argument("--principal", required=True)
    add.add_argument("--branch", default="main")
    sync = commands.add_parser("sync")
    sync.add_argument("root", type=Path)
    sync.add_argument("name")
    check = commands.add_parser("check")
    check.add_argument("root", type=Path)
    commands.add_parser("new-change-id")
    work = commands.add_parser("work")
    work.add_argument("root", type=Path)
    work.add_argument("--limit", type=int)
    canonical = commands.add_parser("update-canonical")
    canonical.add_argument("root", type=Path)
    canonical.add_argument("expected")
    canonical.add_argument("new")
    canonical.add_argument("--principal", required=True)
    transition = commands.add_parser("transition-principal")
    transition.add_argument("root", type=Path)
    transition.add_argument("principal")
    pre = commands.add_parser("pre-receive")
    pre.add_argument("root", type=Path)
    pre.add_argument("name")
    post = commands.add_parser("post-receive")
    post.add_argument("root", type=Path)
    post.add_argument("name")
    token_command = commands.add_parser("add-token")
    token_command.add_argument("root", type=Path)
    token_command.add_argument("view")
    serve = commands.add_parser("serve")
    serve.add_argument("root", type=Path)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "init":
            init_forge(args.root, args.source, args.branch)
        elif args.command == "add-principal":
            _add_principal(args.root, args.principal, args.email)
        elif args.command == "add-service-account":
            _add_service_account(args.root, args.service, args.owner, args.email)
        elif args.command == "add-team":
            _add_team(args.root, args.team, args.member)
        elif args.command == "add-role":
            _add_role(
                args.root,
                args.role,
                capability_grants(args.allow),
                capability_grants(args.deny),
            )
        elif args.command == "bind-role":
            if args.principal is not None:
                _bind_role(args.root, args.role, "principal", args.principal)
            else:
                _bind_role(args.root, args.role, "team", args.team)
        elif args.command == "add-view":
            add_view(args.root, args.name, args.principal, args.branch)
        elif args.command == "sync":
            sync_view(args.root, args.name)
        elif args.command == "check":
            check_invariants(args.root)
        elif args.command == "new-change-id":
            print(new_change_id())
        elif args.command == "work":
            drain_projection_jobs(args.root, args.limit)
        elif args.command == "update-canonical":
            update_canonical_ref(args.root, args.expected, args.new, args.principal)
        elif args.command == "transition-principal":
            transition_principal(args.root, args.principal)
        elif args.command == "pre-receive":
            pre_receive(args.root, args.name, sys.stdin.buffer.read())
        elif args.command == "post-receive":
            post_receive(args.root, args.name, sys.stdin.buffer.read())
        elif args.command == "add-token":
            from http_server import add_token

            print(add_token(args.root, args.view))
        elif args.command == "serve":
            from http_server import serve

            serve(args.root, args.host, args.port)
        return 0
    except ForgeError as error:
        print(f"bastion: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
