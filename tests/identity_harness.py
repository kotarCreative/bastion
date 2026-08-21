"""Shared version-2 fixture support for tests.

Creates one immutable principal, role, and binding per view name, then
materializes the view through the production service API.  Production
``add_view`` itself only ever accepts a principal; this helper exists so
fixtures can keep describing policies as read/write prefix lists.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from service import add_view as _service_add_view  # noqa: E402


ENTRYPOINT = Path(__file__).resolve().parent.parent / "forge.py"


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
            add_role(root, role_id, list(read), list(write))
            bind_role(root, role_id, "principal", name)
    _service_add_view(root, name, owner, branch, ENTRYPOINT)
