"""Operator resource limits: defaults, units, and startup validation.

Every HTTP, receive-validation, and projection limit used by the hosted
services is declared here so documentation and enforcement stay aligned.
Invalid or unsafe environment overrides fail startup instead of being
silently accepted.  Units are bytes unless the name says otherwise.
"""

from __future__ import annotations

import os
import re
import shutil

from errors import ForgeError

_INTEGER = re.compile(r"[0-9]{1,12}")


def _limit(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    if not _INTEGER.fullmatch(raw.strip()):
        raise ForgeError(f"invalid resource limit {name}={raw!r}")
    value = int(raw)
    if value < minimum:
        raise ForgeError(f"unsafe resource limit {name}={value}")
    return value


# HTTP request framing.
UPLOAD_BODY_BYTES = _limit("BASTION_UPLOAD_BODY_BYTES", 32 << 20)
RECEIVE_BODY_BYTES = _limit("BASTION_RECEIVE_BODY_BYTES", 32 << 20)
MAX_CHUNKS = _limit("BASTION_MAX_CHUNKS", 8192)
MAX_TRAILERS = _limit("BASTION_MAX_TRAILERS", 64)
MAX_REQUEST_SECONDS = _limit("BASTION_MAX_REQUEST_SECONDS", 600)
GZIP_MAX_RATIO = _limit("BASTION_GZIP_MAX_RATIO", 32, minimum=2)

# Receive-pack admission budgets.
RECEIVE_PACK_BYTES = _limit("BASTION_MAX_PACK_BYTES", 32 << 20)
MAX_PUSH_COMMITS = _limit("BASTION_MAX_PUSH_COMMITS", 100)
MAX_PUSH_OBJECTS = _limit("BASTION_MAX_PUSH_OBJECTS", 5000)
MAX_OBJECT_BYTES = _limit("BASTION_MAX_OBJECT_BYTES", 4 << 20)
MAX_PUSH_BYTES = _limit("BASTION_MAX_PUSH_BYTES", 64 << 20)

# Publication and volume budgets.
MAX_WAL_SEQUENCE = _limit("BASTION_MAX_WAL_SEQUENCE", 1000)
FREE_FLOOR_BYTES = _limit("BASTION_FREE_FLOOR_BYTES", 1 << 30)
RESERVE_BYTES = _limit("BASTION_RESERVE_BYTES", 512 << 20)

# Concurrency.
MAX_ACTIVE_REQUESTS = _limit("BASTION_MAX_ACTIVE_REQUESTS", 32)
MAX_ACTIVE_GIT = _limit("BASTION_MAX_ACTIVE_GIT", 16)
MAX_ACTIVE_RECEIVES = _limit("BASTION_MAX_ACTIVE_RECEIVES", 4)
MAX_ACTIVE_PER_IP = _limit("BASTION_MAX_ACTIVE_PER_IP", 8)
MAX_ACTIVE_PER_TOKEN = _limit("BASTION_MAX_ACTIVE_PER_TOKEN", 4)
MAX_RECEIVES_PER_IP = _limit("BASTION_MAX_RECEIVES_PER_IP", 2)
MAX_RECEIVES_PER_TOKEN = _limit("BASTION_MAX_RECEIVES_PER_TOKEN", 1)

# Git subprocess deadlines (seconds) and streaming bounds.
GIT_UPLOAD_TIMEOUT = _limit("BASTION_GIT_UPLOAD_TIMEOUT", 300)
GIT_RECEIVE_TIMEOUT = _limit("BASTION_GIT_RECEIVE_TIMEOUT", 600)
GIT_COMMAND_TIMEOUT = _limit("BASTION_GIT_COMMAND_TIMEOUT", 300)
GIT_GRACE_SECONDS = _limit("BASTION_GIT_GRACE_SECONDS", 10)
STDOUT_CHUNK_BYTES = 64 << 10
STDERR_KEEP_BYTES = 64 << 10

# Projection transfer batches.
BLOB_BATCH_OBJECTS = _limit("BASTION_BLOB_BATCH_OBJECTS", 50)
BLOB_BATCH_BYTES = _limit("BASTION_BLOB_BATCH_BYTES", 8 << 20)


def validate() -> None:
    """Cross-check limits so unsafe combinations fail startup."""
    if MAX_OBJECT_BYTES > MAX_PUSH_BYTES:
        raise ForgeError("BASTION_MAX_OBJECT_BYTES exceeds BASTION_MAX_PUSH_BYTES")
    if RECEIVE_PACK_BYTES > RECEIVE_BODY_BYTES:
        raise ForgeError(
            "BASTION_MAX_PACK_BYTES exceeds the receive body limit"
        )
    if MAX_ACTIVE_RECEIVES > MAX_ACTIVE_GIT:
        raise ForgeError(
            "BASTION_MAX_ACTIVE_RECEIVES exceeds the Git subprocess limit"
        )
    if MAX_RECEIVES_PER_TOKEN > MAX_ACTIVE_PER_TOKEN:
        raise ForgeError(
            "BASTION_MAX_RECEIVES_PER_TOKEN exceeds the per-token request limit"
        )


def free_below_floor(path: os.PathLike | str) -> bool:
    """Whether free space at ``path`` is below the floor plus receive reserve."""
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return True
    return usage.free < FREE_FLOOR_BYTES + RESERVE_BYTES
