"""Database roles and cluster bootstrap (SEC-09, technical design §9).

``migrator`` owns every table and runs only in CI and deploys. Each service connects as its own
role, which owns nothing and lacks BYPASSRLS:

- ``app_user`` for ``app``, which serves people;
- ``app_worker`` for ``worker``, which handles untrusted content and holds no credentials;
- ``gw`` for ``gateway``, the one service that holds credentials.

Roles are cluster-wide, so bootstrap runs once per cluster as an administrator, before any
migration. In AWS the services authenticate with IAM (no password); locally each role gets a
password from the environment.
"""

from collections.abc import Mapping
from enum import StrEnum

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url


class Role(StrEnum):
    MIGRATOR = "migrator"
    APP_USER = "app_user"
    APP_WORKER = "app_worker"
    GW = "gw"


SERVICE_ROLES: tuple[Role, ...] = (Role.APP_USER, Role.APP_WORKER, Role.GW)


class DefinerRole(StrEnum):
    """Roles that own SECURITY DEFINER functions. Nobody logs in as them.

    A definer function runs with its owner's privileges, so each owner holds only what its
    functions need. ``ledger_definer`` is still bound by tenant RLS; ``sweeper`` alone may list
    identifiers across tenants, so the worker can find due deadlines.
    """

    LEDGER = "ledger_definer"
    SWEEPER = "sweeper"


def libpq_url(url: str) -> str:
    """Turn a SQLAlchemy URL (``postgresql+psycopg://…``) into a libpq connection string."""
    return make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)


def bootstrap_cluster(
    admin_url: str,
    database: str,
    passwords: Mapping[Role, str] | None = None,
) -> None:
    """Create the login roles, the definer roles and the database, idempotently.

    ``admin_url`` is a SQLAlchemy URL for a role that can create roles and databases (the local
    superuser, or Aurora's master user). Roles in ``passwords`` get that password; the others
    get none and must authenticate some other way (IAM in AWS).
    """
    passwords = passwords or {}
    with psycopg.connect(libpq_url(admin_url), autocommit=True) as conn:
        for role in Role:
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (role.value,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Identifier(role.value))
                )
            if role in passwords:
                conn.execute(
                    sql.SQL("ALTER ROLE {} PASSWORD {}").format(
                        sql.Identifier(role.value), sql.Literal(passwords[role])
                    )
                )

        for definer in DefinerRole:
            exists = conn.execute(
                "SELECT 1 FROM pg_roles WHERE rolname = %s", (definer.value,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    sql.SQL(
                        "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Identifier(definer.value))
                )
            # migrator creates the definer's schema and functions as that role.
            conn.execute(
                sql.SQL("GRANT {} TO {}").format(
                    sql.Identifier(definer.value), sql.Identifier(Role.MIGRATOR.value)
                )
            )

        db_exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
        ).fetchone()
        if db_exists is None:
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(Role.MIGRATOR.value)
                )
            )

        conn.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        conn.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database),
                sql.SQL(", ").join(sql.Identifier(r.value) for r in SERVICE_ROLES),
            )
        )
