"""Initialize an empty local host and approve its configured administrator."""
import os
from pathlib import Path

from host import Host
from cloudflare_host import bootstrap_account


def main():
    root = Path(os.environ.get("BASTION_ROOT", "/var/lib/bastion/host"))
    host = Host(root)
    bootstrap_account(host)
    print("Host initialized. Sign in through Clerk; no repository has been seeded.")


if __name__ == "__main__":
    main()
