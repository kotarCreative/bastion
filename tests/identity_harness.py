"""Shared fixture support for tests.

Creates one immutable principal, role, and binding per view name, then
materializes the view through the production service API.  Production
``add_view`` itself only ever accepts a principal; this helper exists so
fixtures can keep describing policies as read/write prefix lists, which
are expanded into capability grants.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from service import add_view as _service_add_view  # noqa: E402


ENTRYPOINT = Path(__file__).resolve().parent.parent / "forge.py"

WRITE_CAPABILITIES = ("create", "modify", "delete", "mode", "symlink")

ADMIN_PRINCIPAL = "forge-admin"


def capability_grants(read: list[str], write: list[str]) -> tuple[dict, dict]:
    """Expand legacy read/write prefix lists into v3 allow/deny maps."""
    allow = {capability: [] for capability in
             ("read", "create", "modify", "delete", "mode", "symlink",
              "branch", "merge", "admin")}
    deny = {capability: [] for capability in allow}
    allow["read"] = list(read)
    for capability in WRITE_CAPABILITIES:
        allow[capability] = list(write)
    return allow, deny


def provision_admin(root: Path, principal_id: str = ADMIN_PRINCIPAL) -> str:
    """Provision a principal holding root admin coverage over every path."""
    from errors import ForgeError
    from service import add_principal, add_role, bind_role

    try:
        add_principal(root, principal_id)
    except ForgeError as error:
        if "already exists" not in str(error):
            raise
    allow, deny = capability_grants([], [])
    allow["admin"] = [""]
    try:
        add_role(root, f"root-{principal_id}", allow, deny)
    except ForgeError as error:
        if "already exists" not in str(error):
            raise
    try:
        bind_role(root, f"root-{principal_id}", "principal", principal_id)
    except ForgeError as error:
        if "already exists" not in str(error):
            raise
    return principal_id


def update_canonical(
    root: Path,
    expected: str,
    new: str,
    acting_principal: str = ADMIN_PRINCIPAL,
) -> None:
    """Administrative canonical fast-forward by the fixture administrator."""
    from service import update_canonical_ref

    update_canonical_ref(Path(root), expected, new, acting_principal)


def add_view(
    root: Path,
    name: str,
    read: list[str],
    write: list[str],
    branch: str = "main",
    principal: str | None = None,
) -> None:
    """Materialize one view.

    With ``principal`` unset this provisions a dedicated principal named
    ``name`` plus one immutable role from the prefix lists.  With an
    explicit ``principal`` the view binds that existing identity and no
    catalog records are touched.
    """
    from errors import ForgeError
    from service import add_principal, add_role, bind_role

    root = Path(root)
    owner = principal or name
    if principal is None:
        try:
            add_principal(root, name)
        except ForgeError as error:
            if "already exists" not in str(error):
                raise
        if read or write:
            role_id = f"legacy-{name}"
            allow, deny = capability_grants(list(read), list(write))
            add_role(root, role_id, allow, deny)
            bind_role(root, role_id, "principal", name)
    _service_add_view(root, name, owner, branch, ENTRYPOINT)
