"""WSGI request serialization and no-single-repository fallback."""
import os
import threading
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from cloudflare_runtime import HostRuntime, build_app


class CloudflareRuntimeTest(unittest.TestCase):
    def test_local_bootstrap_creates_only_admin_and_preserves_its_identity(self):
        from host import Host
        from cloudflare_host import bootstrap_account
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"BASTION_BOOTSTRAP_ADMIN_EMAIL": "admin@example.test"}):
            host = Host(Path(directory) / "host")
            bootstrap_account(host)
            with host._connect(read_only=True) as con:
                admin = con.execute("SELECT account_id FROM accounts").fetchone()[0]
            self.assertTrue(host.is_host_admin(admin))
            with patch.dict(os.environ, {"BASTION_BOOTSTRAP_ADMIN_EMAIL": "someone-else@example.test"}):
                bootstrap_account(host)
            self.assertTrue(host.is_host_admin(admin))
            self.assertEqual([], host.list_repos())

    def test_requires_durable_host_storage(self):
        with patch.dict(os.environ, {"BASTION_DATABASE": "sqlite"}):
            with self.assertRaisesRegex(RuntimeError, "Durable Object"):
                build_app()

    def test_streams_and_closes_wsgi_response_without_buffering(self):
        closed = []
        def app(environ, start_response):
            start_response("200 OK", [])
            def stream():
                try:
                    yield b"first"
                    yield b"second"
                finally:
                    closed.append(True)
            return stream()
        runtime = HostRuntime(app, object())
        stream = runtime({}, lambda *args: None)
        self.assertEqual(b"first", next(stream))
        self.assertEqual([], closed)
        stream.close()
        self.assertEqual([True], closed)
