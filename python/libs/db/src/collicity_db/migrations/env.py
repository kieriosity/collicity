"""Alembic environment. Migrations are hand-written SQL; there is no autogenerate target."""

from alembic import context
from sqlalchemy import create_engine, pool

url = context.config.get_main_option("sqlalchemy.url")
if not url:
    raise RuntimeError("sqlalchemy.url is not set; run migrations through collicity_db.migrate")
if context.is_offline_mode():
    raise RuntimeError("Offline migrations aren't supported")

engine = create_engine(url, poolclass=pool.NullPool)
with engine.connect() as connection:
    context.configure(connection=connection, transaction_per_migration=True)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
