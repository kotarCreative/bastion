"""Push-transport attachment helpers for test fixtures.

Process-oriented fixtures push to the local intake repository path so
receive-pack runs inside the driving process tree and inherits test
environment context (failpoints, tracing). Transport-oriented tests use
``LoopbackTransport`` to exercise the authenticated HTTP boundary.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from http_server import add_token, make_server  # noqa: E402
from layout import intake_repo  # noqa: E402
from repository import Repository  # noqa: E402


def attach_intake_push(forge_root: Path, checkout: Path, view: str) -> None:
    """Point origin's push URL at the view's intake repository."""
    _manifest, _version, replay = Repository.open(forge_root).load()
    principal = replay.active_views[view]["principal_id"]
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "remote",
            "set-url",
            "--push",
            "origin",
            str(intake_repo(forge_root, view)),
        ],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(checkout),
            "config",
            "remote.origin.receivepack",
            f"env BASTION_VIEW={view} BASTION_PRINCIPAL_ID={principal} "
            "BASTION_TRANSPORT=test git-receive-pack",
        ],
        check=True,
    )


class LoopbackTransport:
    def __init__(self, root: Path):
        self.root = root
        self.server = make_server(root, "127.0.0.1", 0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        self._tokens: dict[str, str] = {}

    def token(self, view: str) -> str:
        if view not in self._tokens:
            self._tokens[view] = add_token(self.root, view)
        return self._tokens[view]

    def url(self, view: str) -> str:
        return f"http://bastion:{self.token(view)}@127.0.0.1:{self.port}/{view}.git"

    def attach(self, checkout: Path, view: str) -> None:
        """Point origin's push URL at the authenticated intake boundary."""
        subprocess.run(
            [
                "git",
                "-C",
                str(checkout),
                "remote",
                "set-url",
                "--push",
                "origin",
                self.url(view),
            ],
            check=True,
        )

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
