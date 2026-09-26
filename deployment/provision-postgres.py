#!/usr/bin/env python3
"""Provision the restricted PostgreSQL role and root-scoped database."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import psycopg
from psycopg import sql


def scoped_database(root: Path) -> str:
    digest = hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:16]
    return f"bastion_{digest}"


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: provision-postgres.py <forge-root>")

    admin_dsn = os.environ.get("BASTION_ADMIN_DATABASE", "")
    runtime_dsn = os.environ.get("BASTION_DATABASE", "")
    if not admin_dsn or not runtime_dsn:
        raise SystemExit("BASTION_ADMIN_DATABASE and BASTION_DATABASE are required")

    runtime = urlsplit(runtime_dsn)
    role = unquote(runtime.username or "")
    password = unquote(runtime.password or "")
    if runtime.scheme not in {"postgres", "postgresql"} or not role or not password:
        raise SystemExit("BASTION_DATABASE must contain a PostgreSQL username and password")

    database = scoped_database(Path(sys.argv[1]))
    with psycopg.connect(admin_dsn, autocommit=True) as connection:
        exists = connection.execute(
            "SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)
        ).fetchone()
        if exists:
            connection.execute(
                sql.SQL(
                    "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password))
            )
        else:
            connection.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Identifier(role), sql.Literal(password))
            )

        database_exists = connection.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if database_exists:
            connection.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
        else:
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )

    print(f"PostgreSQL runtime database {database} is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
