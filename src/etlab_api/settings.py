"""Explicit, validated configuration. Secrets never appear in validation diagnostics."""

from typing import Annotated, Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Csv = Annotated[tuple[str, ...], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
        frozen=True,
    )
    app_mode: Literal["personal", "multiuser"] = "personal"
    api_token: SecretStr
    etlab_base_url: str = "https://sahrdaya.etlab.in"
    etlab_username: SecretStr | None = None
    etlab_password: SecretStr | None = None
    etlab_allowed_hosts: Csv = ("*.etlab.in",)
    etlab_attachment_hosts: Csv = ()
    cache_ttl_seconds: int = 60
    cache_max_bytes: int = 8 * 1024 * 1024
    session_idle_seconds: int = 1800
    session_max_seconds: int = 28800
    upstream_timeout_seconds: float = 20
    upstream_interval_seconds: float = 1
    max_download_bytes: int = 25 * 1024 * 1024
    max_html_bytes: int = 5 * 1024 * 1024
    max_sessions: int = 100
    login_attempts_per_minute: int = 5
    cors_origins: Csv = ()
    docs_enabled: bool = True

    @field_validator("etlab_allowed_hosts", "etlab_attachment_hosts", "cors_origins", mode="before")
    @classmethod
    def split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @field_validator("api_token")
    @classmethod
    def strong_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError(
                "API_TOKEN must contain at least 32 characters; generate a random token"
            )
        return value

    @model_validator(mode="after")
    def check_configuration(self) -> "Settings":
        if self.app_mode == "personal" and not all(
            value and value.get_secret_value().strip()
            for value in (self.etlab_username, self.etlab_password)
        ):
            raise ValueError("Personal mode requires ETLAB_USERNAME and ETLAB_PASSWORD")
        for name in (
            "cache_max_bytes",
            "session_idle_seconds",
            "session_max_seconds",
            "max_download_bytes",
            "max_html_bytes",
            "max_sessions",
            "login_attempts_per_minute",
        ):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.cache_ttl_seconds < 0 or self.upstream_interval_seconds < 0:
            raise ValueError("Cache TTL and upstream interval must be nonnegative")
        if self.upstream_timeout_seconds <= 0:
            raise ValueError("Upstream timeout must be positive")
        if self.session_idle_seconds > self.session_max_seconds:
            raise ValueError("Idle expiry must not exceed maximum session lifetime")
        if not self.etlab_allowed_hosts:
            raise ValueError("At least one allowed ETLab hostname is required")
        return self
