"""Run the real DO SQL API locally with Wrangler/workerd (no Docker)."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

from durable_store import CatalogClient

PROJECT = Path(__file__).resolve().parent.parent / "deployment" / "cloudflare"
AVAILABLE = (PROJECT / "node_modules" / ".bin" / "wrangler").exists()


class DurableObjectsHarness:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="bastion-do-test-")
        with socket.socket() as socket_:
            socket_.bind(("127.0.0.1", 0))
            self.port = socket_.getsockname()[1]
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self.client = CatalogClient(self.endpoint)
        self.process = None
        self.start()

    def start(self):
        self.log = tempfile.TemporaryFile(mode="w+b")
        self.process = subprocess.Popen([
            str(PROJECT / "node_modules" / ".bin" / "wrangler"),
            "dev", "--config", "wrangler.test.jsonc", "--port", str(self.port),
            "--ip", "127.0.0.1", "--persist-to", str(Path(self.temporary.name) / "storage"),
        ], cwd=PROJECT, stdout=self.log, stderr=self.log, start_new_session=True)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.log.seek(0)
                raise RuntimeError("Wrangler test runtime stopped: " + self.log.read().decode(errors="replace")[-2000:])
            try:
                self.client.status()
                return
            except Exception:
                time.sleep(0.1)
        self.stop()
        raise RuntimeError("Wrangler catalog runtime did not become ready")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.log.close()
        self.process = None

    def restart(self):
        self.stop()
        self.start()

    def close(self):
        self.stop()
        self.temporary.cleanup()
