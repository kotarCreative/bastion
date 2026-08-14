"""Inert-by-default named checkpoint seam for real-process testing.

A checkpoint publishes a versioned marker and stops its own process with
``SIGSTOP`` only when explicitly armed through the environment.  The test
controller then decides whether to resume the barrier with ``SIGCONT`` or to
inject a crash with ``SIGKILL``.
"""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path


_NAME_PREFIX = "BASTION_TEST_FAILPOINT"


def _process_start_time(pid: int) -> str | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
    except OSError:
        return None
    return fields[21] if len(fields) > 21 else None


def _atomic_marker(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o644,
    )
    try:
        data = json.dumps(payload, indent=2, sort_keys=True).encode() + b"\n"
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def checkpoint(root: Path, name: str) -> None:
    """Pause at a named test checkpoint when explicitly armed."""
    present = {
        key: os.environ.get(key)
        for key in (
            f"{_NAME_PREFIX}",
            f"{_NAME_PREFIX}_ROOT",
            f"{_NAME_PREFIX}_MARKER",
        )
    }
    values = list(present.values())

    if all(value is None for value in values):
        return
    if not all(value is not None for value in values):
        raise RuntimeError(
            f"incomplete { _NAME_PREFIX } configuration: "
            f"need all of {', '.join(present)} for checkpoint {name!r}"
        )

    if Path(values[1]).resolve() != root.resolve():
        return
    if values[0] != name:
        return

    marker = Path(values[2])
    pid = os.getpid()
    start = _process_start_time(pid)
    if start is None:
        raise RuntimeError(
            f"checkpoint {name!r} cannot read /proc/{pid}/stat for start identity"
        )

    payload = {
        "version": 1,
        "checkpoint": name,
        "root": str(root.resolve()),
        "pid": pid,
        "process_start": start,
    }

    # Removing the variables guarantees the same process cannot fire twice.
    for key in present:
        os.environ.pop(key, None)

    _atomic_marker(marker, payload)
    os.kill(pid, signal.SIGSTOP)
