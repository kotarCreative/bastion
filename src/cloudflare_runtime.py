"""Cloudflare multi-repository entry point; no automatic repository seeding."""
from __future__ import annotations

import os
import threading
from pathlib import Path


def build_store():
    from object_store_artifacts import ArtifactsObjectStore, ArtifactsObjectStoreConfig

    remote = os.environ.get("BASTION_ARTIFACTS_REMOTE", "")
    if not remote or not os.environ.get("BASTION_ARTIFACTS_TOKEN"):
        raise RuntimeError("Artifacts remote and private write token are required")
    config = ArtifactsObjectStoreConfig(remote, branch="bastion-host-storage")
    return ArtifactsObjectStore.from_config(
        config, cache_dir=Path(os.environ.get("BASTION_ARTIFACTS_CACHE", "/tmp/bastion-artifacts-cache")))


class HostRuntime:
    """One Container/process owns host provisioning; DO SQL is always durable.

    Keep the request and iterable in the same serialized boundary. No mutable
    server configuration or process-wide repository credential context is swapped
    between repositories; receive hooks read the explicit scoped backend record.
    """
    def __init__(self, app, host):
        self.app, self.host = app, host
        self.lock = threading.RLock()

    def __call__(self, environ, start_response):
        def respond():
            with self.lock:
                iterable = self.app(environ, start_response)
                try:
                    yield from iterable
                finally:
                    close = getattr(iterable, "close", None)
                    if close:
                        close()
        return respond()


def build_app():
    from cloudflare_host import CloudflareHost, bootstrap_account
    from host_server import create_app

    if os.environ.get("BASTION_DATABASE") != "durable_objects":
        raise RuntimeError("Cloudflare hosting requires Durable Object catalogs")
    root = Path(os.environ.get("BASTION_ROOT", "/var/lib/bastion/host"))
    host = CloudflareHost(root)
    bootstrap_account(host)
    return HostRuntime(create_app(root, host_catalog=host), host)
