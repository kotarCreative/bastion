"""Idempotently install the catalog and views for the Docker test profile."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from http_server import add_token, revoke_token
from identity import CAPABILITIES, binding_identifier, effective_descriptor
from invariants import check_invariants
from repository import Repository
from service import add_principal, add_role, add_view, bind_role
from service import transition_principal
from state import load_state
from worker import drain_projection_jobs


def capability_map(**grants: list[str]) -> dict[str, list[str]]:
    return {capability: list(grants.get(capability, [])) for capability in CAPABILITIES}


def store_rotated_tokens(
    root: Path,
    secrets_client,
    secret_arn: str,
    tester: str,
    observer: str,
    previous: dict,
) -> None:
    previous_credentials = [
        value
        for key, value in previous.items()
        if key in {"tester_token", "observer_token"} and isinstance(value, str)
    ]
    superseded_token_ids = [
        value
        for value in previous.get("superseded_token_ids", [])
        if isinstance(value, str)
    ]
    for credential in previous_credentials:
        parts = credential.split(".", 2)
        if len(parts) == 3 and parts[0] == "bst1":
            superseded_token_ids.append(parts[1])
    superseded_token_ids = sorted(set(superseded_token_ids))

    current = {"tester_token": tester, "observer_token": observer}
    pending = dict(current)
    if superseded_token_ids:
        pending["superseded_token_ids"] = superseded_token_ids
    try:
        secrets_client.put_secret_value(
            SecretId=secret_arn,
            SecretString=json.dumps(pending, sort_keys=True),
        )
    except Exception as write_error:
        try:
            observed = json.loads(
                secrets_client.get_secret_value(SecretId=secret_arn)["SecretString"]
            )
        except Exception:
            # The write outcome is ambiguous. Keep both old and new credentials
            # valid rather than risk publishing revoked credentials.
            raise write_error
        if observed != pending:
            for credential in (tester, observer):
                revoke_token(root, credential.split(".", 2)[1])
            raise write_error

    for token_id in superseded_token_ids:
        revoke_token(root, token_id)
    if superseded_token_ids:
        secrets_client.put_secret_value(
            SecretId=secret_arn,
            SecretString=json.dumps(current, sort_keys=True),
        )


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

    state = load_state(root)
    if "tester-owner" not in state["roles"]:
        add_role(
            root,
            "tester-owner",
            capability_map(admin=[""]),
            capability_map(),
        )
    state = load_state(root)
    owner_binding = binding_identifier("principal", "tester", "tester-owner")
    if owner_binding not in state["role_bindings"]:
        bind_role(root, "tester-owner", "principal", "tester")

    replay = Repository.open(root).load()[2]
    if "tester" not in replay.active_views:
        add_view(root, "tester", "tester", "main")
    replay = Repository.open(root).load()[2]
    if "observer" not in replay.active_views:
        add_view(root, "observer", "observer", "main")
    replay = Repository.open(root).load()[2]
    state = load_state(root)
    fresh = effective_descriptor(
        state, "tester", f"refs/heads/{state['canonical_branch']}"
    )
    if replay.active_views["tester"]["descriptor_id"] != fresh["id"]:
        transition_principal(root, "tester")

def main() -> int:
    root = Path(sys.argv[1])
    ensure_catalog(root)
    drain_projection_jobs(root)
    check_invariants(root)
    secret_arn = os.environ.get("BASTION_BOOTSTRAP_TOKENS_SECRET_ARN", "")
    secrets_client = None
    previous: dict = {}
    if secret_arn:
        import boto3

        secrets_client = boto3.client("secretsmanager")
        try:
            previous = json.loads(
                secrets_client.get_secret_value(SecretId=secret_arn)["SecretString"]
            )
            if not isinstance(previous, dict):
                raise ValueError("bootstrap token secret must contain a JSON object")
        except secrets_client.exceptions.ResourceNotFoundException:
            pass

    tester = add_token(root, "tester", ["fetch", "push", "ui"], "7d")
    observer = add_token(root, "observer", ["fetch", "ui"], "7d")
    print("Bastion test data is ready.")
    if secret_arn:
        assert secrets_client is not None
        store_rotated_tokens(
            root, secrets_client, secret_arn, tester, observer, previous
        )
        print(f"Git/API tokens were written to Secrets Manager: {secret_arn}")
    else:
        print("Save these local credentials; they are shown once:")
        print(f"export BASTION_TESTER_TOKEN='{tester}'")
        print(f"export BASTION_OBSERVER_TOKEN='{observer}'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
