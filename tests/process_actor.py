#!/usr/bin/env python3
"""Subprocess entrypoint for deterministic process tests.

This module is intentionally not named ``test_*.py``.  It provides small,
focused actors that the process harness launches as real child processes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from state import locked
from worker import run_projection_job


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(
            "usage: process_actor.py {worker|recover|fail} ROOT ...",
            file=sys.stderr,
        )
        return 2
    command = argv[0]
    root = Path(argv[1])
    if command == "worker":
        if len(argv) != 3:
            print("usage: process_actor.py worker ROOT NOW", file=sys.stderr)
            return 2
        run_projection_job(root, now=float(argv[2]))
        return 0
    if command == "recover":
        if len(argv) != 2:
            print("usage: process_actor.py recover ROOT", file=sys.stderr)
            return 2
        with locked(root):
            pass
        return 0
    if command == "fail":
        if len(argv) != 5:
            print(
                "usage: process_actor.py fail ROOT NOW JOB_ID TOKEN",
                file=sys.stderr,
            )
            return 2
        from worker import fail_projection_job

        fail_projection_job(
            root,
            {"id": argv[3], "lease_token": argv[4]},
            RuntimeError("injected process failure"),
            now=float(argv[2]),
        )
        return 0
    print(f"unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
