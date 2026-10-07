"""External browser identities bound to immutable user principals."""

from __future__ import annotations

from errors import ForgeError
from identity import normalize_emails
from state import audit_event, locked, save_state


class AccountLinkError(ForgeError):
    """A verified external identity has no unambiguous Bastion invitation."""


def link_clerk_account(root, clerk_user_id: str, verified_emails: list[str]) -> str:
    """Resolve or atomically link one Clerk user to an invited principal."""
    if (
        not isinstance(clerk_user_id, str)
        or not clerk_user_id.startswith("user_")
        or len(clerk_user_id) > 255
    ):
        raise ForgeError("invalid Clerk user identity")
    emails = set(normalize_emails(verified_emails))
    if not emails:
        raise ForgeError("Clerk account has no verified email")
    with locked(root) as state:
        principals = state["principals"]
        linked = [
            principal
            for principal in principals.values()
            if principal.get("clerk_user_id") == clerk_user_id
        ]
        if len(linked) > 1:
            raise ForgeError("Clerk identity is linked more than once", "integrity")
        if linked:
            principal = linked[0]
            if principal.get("kind") != "user":
                raise ForgeError("Clerk identity is linked to a non-user", "integrity")
            return principal["id"]
        candidates = [
            principal
            for principal in principals.values()
            if principal.get("kind") == "user"
            and emails.intersection(principal.get("emails", []))
            and "clerk_user_id" not in principal
        ]
        if not candidates:
            raise AccountLinkError("Clerk account has no Bastion invitation")
        if len(candidates) > 1:
            raise AccountLinkError("verified email matches multiple Bastion principals")
        principal = candidates[0]
        principal["clerk_user_id"] = clerk_user_id
        save_state(root, state)
        audit_event(
            root,
            "account.clerk.link",
            principal["id"],
            "verified Clerk identity linked",
        )
        return principal["id"]
