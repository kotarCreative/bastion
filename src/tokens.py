"""Digest-only hosted bearer tokens (spec 013).

The complete credential has the ASCII grammar::

    bst1.<token-id>.<secret>

``token-id`` is 16 random bytes encoded as 22 unpadded base64url
characters. ``secret`` is 32 random bytes encoded as 43 unpadded
base64url characters. Only the SHA256 digest is persisted; raw secrets
are returned once at issuance and never stored or logged.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import time

from errors import ForgeError

TOKEN_RE = re.compile(r"^bst1\.[A-Za-z0-9_-]{22}\.[A-Za-z0-9_-]{43}$")
TOKEN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{22}$")
SCOPES = ("fetch", "push", "ui")
DEFAULT_SCOPES = ("fetch", "ui")
DIGEST_DOMAIN = b"bastion-http-token-v1\0"


def _b64encode_unpadded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode_unpadded(text: str, expected: int) -> bytes:
    if len(text) != expected:
        raise ForgeError("malformed bearer token")
    padded = text + "=" * (-len(text) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (binascii.Error, ValueError, UnicodeEncodeError) as error:
        raise ForgeError("malformed bearer token") from error
    return raw


def generate_token_id() -> str:
    return _b64encode_unpadded(secrets.token_bytes(16))


def generate_secret() -> str:
    return _b64encode_unpadded(secrets.token_bytes(32))


def split_credential(credential: str) -> tuple[str, str]:
    """Return (token_id, secret) or raise ForgeError on grammar failure."""
    if not isinstance(credential, str) or not TOKEN_RE.fullmatch(credential):
        raise ForgeError("malformed bearer token")
    _, token_id, secret = credential.split(".", 2)
    # Impose exact decoded lengths before accepting.
    token_raw = _b64decode_unpadded(token_id, 22)
    secret_raw = _b64decode_unpadded(secret, 43)
    if len(token_raw) != 16 or len(secret_raw) != 32:
        raise ForgeError("malformed bearer token")
    return token_id, secret


def digest(token_id: str, secret: str) -> str:
    material = DIGEST_DOMAIN + token_id.encode("ascii") + b"\0" + secret.encode("ascii")
    return hashlib.sha256(material).hexdigest()


def compare_digest(presented_hex: str, stored_hex: str) -> bool:
    try:
        presented = bytes.fromhex(presented_hex)
        stored = bytes.fromhex(stored_hex)
    except ValueError:
        return False
    if len(presented) != len(stored):
        return False
    return hmac.compare_digest(presented, stored)


# A fixed dummy digest keeps timing for unknown token IDs comparable to
# the known-ID path without persisting any secret material.
DUMMY_DIGEST = "00" * 32


def normalize_scopes(values: object) -> list[str]:
    if not isinstance(values, (list, tuple)) or not values:
        raise ForgeError("token scopes must be a nonempty list")
    unique = sorted(set(values))
    if set(unique) != set(values) or any(
        not isinstance(item, str) or item not in SCOPES for item in values
    ):
        raise ForgeError("token scopes must be a unique subset of fetch, push, ui")
    if not unique or any(item not in SCOPES for item in unique):
        raise ForgeError("token scopes must be a unique subset of fetch, push, ui")
    return unique


def has_scope(record: object, scope: str) -> bool:
    """Whether one authenticated token record grants one required scope.

    Every authenticated surface routes its scope decision through this
    single predicate so required-scope enforcement cannot drift between
    browser, API, advertisement, and Git RPC routes.
    """
    return (
        isinstance(record, dict)
        and scope in record.get("scopes", ())
    )


def parse_expires_in(raw: str | None) -> int | None:
    """Parse ``--expires-in`` durations like ``30m``, ``12h``, ``7d``."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ForgeError("token expiry must look like 30m, 12h, or 7d")
    match = re.fullmatch(r"([1-9][0-9]*)([mhd])", raw.strip())
    if not match:
        raise ForgeError("token expiry must look like 30m, 12h, or 7d")
    amount = int(match.group(1))
    unit = {"m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return int(time.time()) + amount * unit


def validate_record(document: object) -> dict:
    if not isinstance(document, dict):
        raise ForgeError("token record has an invalid shape")
    expected = {
        "token_id",
        "secret_digest",
        "view_id",
        "scopes",
        "created_at",
        "expires_at",
        "revoked_at",
    }
    if set(document) != expected:
        raise ForgeError("token record has an invalid shape")
    token_id = document["token_id"]
    if not isinstance(token_id, str) or not TOKEN_ID_RE.fullmatch(token_id):
        raise ForgeError("token record has an invalid token id")
    _b64decode_unpadded(token_id, 22)
    digest_hex = document["secret_digest"]
    if (
        not isinstance(digest_hex, str)
        or len(digest_hex) != 64
        or any(c not in "0123456789abcdef" for c in digest_hex)
    ):
        raise ForgeError("token record has an invalid digest")
    view_id = document["view_id"]
    if not isinstance(view_id, str) or not view_id:
        raise ForgeError("token record has an invalid view")
    scopes = normalize_scopes(document["scopes"])
    for stamp_key in ("created_at", "expires_at", "revoked_at"):
        stamp = document[stamp_key]
        if stamp is None:
            if stamp_key == "created_at":
                raise ForgeError("token record has an invalid timestamp")
            continue
        if not isinstance(stamp, int) or isinstance(stamp, bool) or stamp < 0:
            raise ForgeError("token record has an invalid timestamp")
    return {
        "token_id": token_id,
        "secret_digest": digest_hex,
        "view_id": view_id,
        "scopes": scopes,
        "created_at": document["created_at"],
        "expires_at": document["expires_at"],
        "revoked_at": document["revoked_at"],
    }


def is_expired(record: dict, now: float | None = None) -> bool:
    expires_at = record.get("expires_at")
    if expires_at is None:
        return False
    return (now if now is not None else time.time()) >= expires_at


def is_revoked(record: dict) -> bool:
    return record.get("revoked_at") is not None
