"""Idempotently install the catalog and views for the Docker test profile."""

from __future__ import annotations

import sys
from pathlib import Path

from http_server import add_token
from identity import CAPABILITIES, binding_identifier
from invariants import check_invariants
from repository import Repository
from service import add_principal, add_role, add_view, bind_role
from state import load_state
from worker import drain_projection_jobs


def capability_map(**grants: list[str]) -> dict[str, list[str]]:
    return {capability: list(grants.get(capability, [])) for capability in CAPABILITIES}


def ensure_catalog(root: Path) -> None:
    state = load_state(root)
    if "tester" not in state["principals"]:
        add_principal(root, "tester", ["tester@bastion.invalid"])
    state = load_state(root)
    if "tester-role" not in state["roles"]:
        add_role(
            root,
            "tester-role",
            capability_map(
                read=[""],
                create=["shared"],
                modify=["shared"],
                delete=["shared"],
                mode=["shared"],
                symlink=["shared"],
            ),
            capability_map(),
        )
    state = load_state(root)
    tester_binding = binding_identifier("principal", "tester", "tester-role")
    if tester_binding not in state["role_bindings"]:
        bind_role(root, "tester-role", "principal", "tester")

    state = load_state(root)
    if "observer" not in state["principals"]:
        add_principal(root, "observer")
    state = load_state(root)
    if "observer-role" not in state["roles"]:
        add_role(
            root,
            "observer-role",
            capability_map(read=["shared"]),
            capability_map(),
        )
    state = load_state(root)
    observer_binding = binding_identifier("principal", "observer", "observer-role")
    if observer_binding not in state["role_bindings"]:
        bind_role(root, "observer-role", "principal", "observer")

    replay = Repository.open(root).load()[2]
    if "tester" not in replay.active_views:
        add_view(root, "tester", "tester", "main")
    replay = Repository.open(root).load()[2]
    if "observer" not in replay.active_views:
        add_view(root, "observer", "observer", "main")


def main() -> int:
    root = Path(sys.argv[1])
    ensure_catalog(root)
    drain_projection_jobs(root)
    check_invariants(root)
    tester = add_token(root, "tester", ["fetch", "push", "ui"], "7d")
    observer = add_token(root, "observer", ["fetch", "ui"], "7d")
    print("Bastion test data is ready. Save these credentials; they are shown once:")
    print(f"export BASTION_TESTER_TOKEN='{tester}'")
    print(f"export BASTION_OBSERVER_TOKEN='{observer}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
