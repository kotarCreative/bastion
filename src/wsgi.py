"""WSGI entry point for the deployed browser and JSON API service."""

from __future__ import annotations

import os
from pathlib import Path

from host_server import create_app


root = os.environ.get("BASTION_ROOT")
if not root:
    raise RuntimeError("BASTION_ROOT is required")

app = create_app(Path(root))
