from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    config_dir: Path = Path("/config")
    tv_dir: Path = Path("/tv")
    downloads_dir: Path = Path("/downloads")

    qbit_url: str = ""
    qbit_username: str = ""
    qbit_password: str = ""
    tvdb_api_key: str = ""
    tvdb_pin: str | None = None
    flaresolverr_url: str | None = None
    apprise_url: str | None = None
    apprise_tag: str | None = None

    oidc_issuer: str | None = None
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_allowed_users: Annotated[list[str], NoDecode] = []
    oidc_allowed_groups: Annotated[list[str], NoDecode] = []
    oidc_provider_name: str = "Pocket ID"  # shown on the login button

    app_url: str = ""
    session_secret: str | None = None
    disable_scheduler: bool = False

    @field_validator("oidc_allowed_users", "oidc_allowed_groups", mode="before")
    @classmethod
    def _split_csv(cls, v):
        if isinstance(v, str):
            return [p.strip() for p in v.split(",") if p.strip()]
        return v

    @property
    def auth_enabled(self) -> bool:
        return bool(self.oidc_issuer)


@lru_cache
def get_settings() -> Settings:
    return Settings()
