"""Database operations: ``python -m collicity_db bootstrap|upgrade``.

bootstrap  Create roles and the database as an administrator. Reads
           COLLICITY_ADMIN_DATABASE_URL, COLLICITY_DATABASE_NAME (default "collicity") and, for
           local development, COLLICITY_ROLE_PASSWORD_<ROLE> (for example
           COLLICITY_ROLE_PASSWORD_APP_USER). Roles without one get no password.
upgrade    Apply migrations as migrator. Reads COLLICITY_MIGRATOR_DATABASE_URL.
"""

import argparse
import os

from collicity_db.migrate import upgrade
from collicity_db.roles import Role, bootstrap_cluster


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m collicity_db")
    parser.add_argument("command", choices=["bootstrap", "upgrade"])
    args = parser.parse_args()

    if args.command == "bootstrap":
        passwords = {
            role: password
            for role in Role
            if (password := os.environ.get(f"COLLICITY_ROLE_PASSWORD_{role.name}"))
        }
        bootstrap_cluster(
            _require("COLLICITY_ADMIN_DATABASE_URL"),
            os.environ.get("COLLICITY_DATABASE_NAME", "collicity"),
            passwords,
        )
    else:
        upgrade(_require("COLLICITY_MIGRATOR_DATABASE_URL"))


if __name__ == "__main__":
    main()
