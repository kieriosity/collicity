from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    """Configuration for ``app``, read from ``COLLICITY_APP_*`` environment variables."""

    model_config = SettingsConfigDict(env_prefix="COLLICITY_APP_", frozen=True)

    # A SQLAlchemy URL for the app_user role. Never the migrator's.
    database_url: SecretStr
    environment: Literal["local", "dev", "staging", "prod"] = "local"
