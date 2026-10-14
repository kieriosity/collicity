"""Migrations: idempotent bootstrap, repeatable upgrade, and mappings that match the schema."""

import secrets

import psycopg
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from psycopg import sql
from sqlalchemy import create_engine

from collicity_db import Role
from collicity_db.migrate import upgrade
from collicity_db.models import Base
from collicity_db.roles import bootstrap_cluster, libpq_url
from collicity_testing.postgres import TEST_PASSWORDS, MigratedDatabase


def test_orm_mappings_match_the_migrated_schema(database: MigratedDatabase):
    engine = create_engine(database.urls[Role.MIGRATOR])
    try:
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    finally:
        engine.dispose()
    # alembic_version is Alembic's own table, not one of ours.
    diff = [d for d in diff if not (d[0] == "remove_table" and d[1].name == "alembic_version")]
    assert diff == []


def test_upgrade_twice_is_a_no_op(database: MigratedDatabase):
    upgrade(database.urls[Role.MIGRATOR])


def test_bootstrap_is_idempotent(database: MigratedDatabase):
    name = f"collicity_test_{secrets.token_hex(4)}"
    server = libpq_url(database.admin_url)
    try:
        bootstrap_cluster(database.admin_url, name, TEST_PASSWORDS)
        bootstrap_cluster(database.admin_url, name, TEST_PASSWORDS)
    finally:
        with psycopg.connect(server, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )
