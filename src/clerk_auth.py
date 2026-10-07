"""Clerk session verification for the hosted browser application."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

from clerk_backend_api import Clerk
from clerk_backend_api.security.types import AuthenticateRequestOptions

from errors import ForgeError


class ClerkAuthenticationError(Exception):
    """A Clerk credential could not be authenticated as an active user."""


@dataclass(frozen=True)
class ClerkConfig:
    publishable_key: str
    secret_key: str
    authorized_parties: tuple[str, ...]
    frontend_api_origin: str
    jwt_key: str | None = None


def _frontend_api_origin(publishable_key: str) -> str:
    try:
        encoded = publishable_key.split("_", 2)[2]
        decoded = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("ascii")
    except (IndexError, ValueError, UnicodeDecodeError) as error:
        raise ForgeError("CLERK_PUBLISHABLE_KEY is invalid") from error
    hostname = decoded.removesuffix("$")
    parsed = urlsplit(f"https://{hostname}")
    if (
        not hostname
        or parsed.scheme != "https"
        or parsed.hostname != hostname
        or parsed.port is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ForgeError("CLERK_PUBLISHABLE_KEY is invalid")
    return f"https://{hostname}"


def load_clerk_config() -> ClerkConfig | None:
    publishable_key = os.environ.get("CLERK_PUBLISHABLE_KEY", "").strip()
    secret_key = os.environ.get("CLERK_SECRET_KEY", "").strip()
    raw_parties = os.environ.get("CLERK_AUTHORIZED_PARTIES", "").strip()
    jwt_key = os.environ.get("CLERK_JWT_KEY", "").strip() or None
    if not any((publishable_key, secret_key, raw_parties, jwt_key)):
        return None
    if not publishable_key or not secret_key or not raw_parties:
        raise ForgeError(
            "Clerk requires CLERK_PUBLISHABLE_KEY, CLERK_SECRET_KEY, and "
            "CLERK_AUTHORIZED_PARTIES"
        )
    parties = tuple(value.strip().rstrip("/") for value in raw_parties.split(","))
    if any(
        not value
        or urlsplit(value).scheme not in {"http", "https"}
        or not urlsplit(value).netloc
        or urlsplit(value).path not in {"", "/"}
        or urlsplit(value).query
        or urlsplit(value).fragment
        for value in parties
    ):
        raise ForgeError("CLERK_AUTHORIZED_PARTIES must contain origin URLs")
    return ClerkConfig(
        publishable_key=publishable_key,
        secret_key=secret_key,
        authorized_parties=parties,
        frontend_api_origin=_frontend_api_origin(publishable_key),
        jwt_key=jwt_key,
    )


class ClerkAuthenticator:
    def __init__(self, config: ClerkConfig):
        self.config = config
        self._client = Clerk(bearer_auth=config.secret_key, timeout_ms=5_000)

    def authenticate(self, headers) -> tuple[str, list[str]]:
        """Return the Clerk user ID and verified email addresses."""
        try:
            state = self._client.authenticate_request(
                _HeaderRequest(headers),
                AuthenticateRequestOptions(
                    jwt_key=self.config.jwt_key,
                    authorized_parties=list(self.config.authorized_parties),
                    accepts_token=["session_token"],
                ),
            )
            payload = state.payload if state.is_signed_in else None
            user_id = payload.get("sub") if isinstance(payload, dict) else None
            if (
                not isinstance(user_id, str)
                or not user_id.startswith("user_")
                or len(user_id) > 255
                or payload.get("sts") == "pending"
            ):
                raise ClerkAuthenticationError("invalid Clerk session")
            user = self._client.users.get(user_id=user_id)
        except ClerkAuthenticationError:
            raise
        except Exception as error:
            raise ClerkAuthenticationError("Clerk authentication failed") from error
        if user.banned or user.locked or user.deprovisioned:
            raise ClerkAuthenticationError("Clerk account is unavailable")
        emails = sorted(
            {
                address.email_address.strip().lower()
                for address in user.email_addresses
                if address.verification is not None
                and getattr(address.verification.status, "value", address.verification.status)
                == "verified"
            }
        )
        if not emails:
            raise ClerkAuthenticationError("Clerk account has no verified email")
        return user_id, emails


class _HeaderRequest:
    def __init__(self, headers):
        self._headers = headers

    @property
    def headers(self):
        return self._headers
