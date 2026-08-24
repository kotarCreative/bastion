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
from protocol import hook_pre_receive, post_receive, pre_receive
from service import (
    add_principal as _add_principal,
    add_role as _add_role,
    add_service_account as _add_service_account,
    add_team as _add_team,
    add_view as _add_view,
    bind_role as _bind_role,
    init_forge,
    migrate_storage,
    sync_view,
    transition_principal,
    update_canonical_ref,
)
from state import save_state
from worker import (
    cancel_projection_job,
    drain_projection_jobs,
    requeue_projection_job,
)


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
    init.add_argument(
        "--database",
        help="storage backend URI: sqlite (default) or postgres://...",
    )
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
    stats = commands.add_parser("stats")
    stats.add_argument("root", type=Path)
    migrate = commands.add_parser("migrate-storage")
    migrate.add_argument("root", type=Path)
    migrate.add_argument(
        "--database",
        help="target storage backend URI: sqlite (default) or postgres://...",
    )
    backup = commands.add_parser("backup")
    backup.add_argument("root", type=Path)
    backup.add_argument("destination", type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("backup", type=Path)
    restore.add_argument("root", type=Path)
    restore.add_argument(
        "--database",
        help="target storage backend URI for the restored forge",
    )
    cancel_job = commands.add_parser("cancel-job")
    cancel_job.add_argument("root", type=Path)
    cancel_job.add_argument("job_id")
    requeue_job = commands.add_parser("requeue-job")
    requeue_job.add_argument("root", type=Path)
    requeue_job.add_argument("job_id")
    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("root", type=Path)
    heartbeat.add_argument("token")
    heartbeat.add_argument("--seconds", type=float)
    audit_command = commands.add_parser("audit")
    audit_command.add_argument("root", type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "init":
            init_forge(args.root, args.source, args.branch, args.database)
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
            raise SystemExit(
                hook_pre_receive(args.root, args.name, sys.stdin.buffer.read())
            )
        elif args.command == "post-receive":
            post_receive(args.root, args.name, sys.stdin.buffer.read())
        elif args.command == "add-token":
            from http_server import add_token

            print(add_token(args.root, args.view))
        elif args.command == "serve":
            from http_server import serve

            serve(args.root, args.host, args.port)
        elif args.command == "stats":
            import json as _json

            from store import open_store

            store = open_store(args.root)
            try:
                print(_json.dumps(store.stats(), indent=2, sort_keys=True))
            finally:
                store.close()
        elif args.command == "migrate-storage":
            migrate_storage(args.root, args.database)
        elif args.command == "backup":
            from store import backup_forge

            print(backup_forge(args.root, args.destination))
        elif args.command == "restore":
            from store import restore_forge

            print(restore_forge(args.backup, args.root, args.database))
        elif args.command == "cancel-job":
            cancel_projection_job(args.root, args.job_id)
        elif args.command == "requeue-job":
            requeue_projection_job(args.root, args.job_id)
        elif args.command == "heartbeat":
            from state import heartbeat_session

            print(heartbeat_session(args.root, args.token, args.seconds))
        elif args.command == "audit":
            from state import load_state as _load

            from store import open_store as _open

            store = _open(args.root)
            try:
                for entry in store.recent_audit(100):
                    print(
                        f"{entry['seq']}\t{entry['at']:.0f}\t{entry['kind']}\t"
                        f"{entry['subject']}\t{entry['detail']}"
                    )
            finally:
                store.close()
        return 0
    except ForgeError as error:
        print(f"bastion: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
