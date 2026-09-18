"""Password-backed browser accounts bound to immutable user principals.

Account credentials live on the principal catalog row, but are deliberately
excluded from effective view descriptors.  Changing a password therefore
never changes repository authorization or a projected Git history.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from errors import ForgeError
from state import audit_event, load_state_row, locked, save_state


ALGORITHM = "scrypt-v1"
SCRYPT_N = 1 << 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32
MIN_PASSWORD_BYTES = 8
MAX_PASSWORD_BYTES = 1024


def _password_bytes(password: object) -> bytes:
    if not isinstance(password, str):
        raise ForgeError("password must be a string")
    encoded = password.encode("utf-8")
    if len(encoded) < MIN_PASSWORD_BYTES:
        raise ForgeError("password must be at least 8 bytes")
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise ForgeError("password is too large")
    return encoded


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _decode(value: object, length: int) -> bytes:
    if not isinstance(value, str):
        raise ForgeError("account credential record is invalid", "integrity")
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError) as error:
        raise ForgeError("account credential record is invalid", "integrity") from error
    if len(raw) != length:
        raise ForgeError("account credential record is invalid", "integrity")
    return raw


def password_record(password: str) -> dict:
    encoded = _password_bytes(password)
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.scrypt(
        encoded, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES
    )
    return {
        "algorithm": ALGORITHM,
        "salt": _b64(salt),
        "digest": _b64(digest),
        "n": SCRYPT_N,
        "r": SCRYPT_R,
        "p": SCRYPT_P,
    }


def validate_record(value: object) -> dict:
    expected = {"algorithm", "salt", "digest", "n", "r", "p"}
    if not isinstance(value, dict) or set(value) != expected:
        raise ForgeError("account credential record is invalid", "integrity")
    if (
        value.get("algorithm") != ALGORITHM
        or value.get("n") != SCRYPT_N
        or value.get("r") != SCRYPT_R
        or value.get("p") != SCRYPT_P
    ):
        raise ForgeError("account credential record is invalid", "integrity")
    _decode(value["salt"], SALT_BYTES)
    _decode(value["digest"], KEY_BYTES)
    return dict(value)


def set_password(root, principal_id: str, password: str) -> None:
    """Create or replace the browser account for one user principal."""
    record = password_record(password)
    with locked(root) as state:
        principal = state["principals"].get(principal_id)
        if principal is None:
            raise ForgeError(f"unknown principal: {principal_id}")
        if principal.get("kind") != "user":
            raise ForgeError("only user principals can have browser accounts")
        principal["account"] = record
        save_state(root, state)
        audit_event(root, "account.password.set", principal_id, "password credential replaced")


def verify_password(root, principal_id: str, password: object) -> bool:
    """Constant-time password verification with a dummy unknown-user path."""
    try:
        encoded = _password_bytes(password)
    except ForgeError:
        # Invalid inputs remain generic to callers and avoid expensive work.
        return False
    principal = load_state_row(root, "principals", principal_id)
    account = principal.get("account") if isinstance(principal, dict) else None
    try:
        validated = validate_record(account)
        salt = _decode(validated["salt"], SALT_BYTES)
        expected = _decode(validated["digest"], KEY_BYTES)
    except ForgeError:
        # The fixed dummy values keep unknown users on the same scrypt path.
        salt = b"\0" * SALT_BYTES
        expected = b"\0" * KEY_BYTES
    actual = hashlib.scrypt(
        encoded, salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=KEY_BYTES
    )
    return account is not None and hmac.compare_digest(actual, expected)
