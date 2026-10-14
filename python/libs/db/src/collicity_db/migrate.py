"""Run Alembic migrations as ``migrator``."""

from alembic import command
from alembic.config import Config


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", "collicity_db:migrations")
    # ConfigParser treats % as interpolation; URL-encoded passwords contain it.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    return config


def upgrade(url: str, revision: str = "head") -> None:
    """Upgrade the database at ``url`` (a SQLAlchemy URL for ``migrator``)."""
    command.upgrade(alembic_config(url), revision)
