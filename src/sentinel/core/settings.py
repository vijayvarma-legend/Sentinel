"""Configuration, validated once at startup.

Settings fail loudly at process start rather than at the moment they are first read. In a
payment system the alternative is worse than it sounds: a missing tolerance or an absent
policy source discovered halfway through an invoice leaves that invoice in an indeterminate
state, and spec §12 requires every workflow to be recoverable.

Everything is read from ``SENTINEL_``-prefixed environment variables (or a local ``.env``),
so nothing secret is ever committed. See ``.env.example``.
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sentinel.core.errors import ConfigurationError

__all__ = ["Settings", "get_settings"]

Environment = Literal["local", "test", "staging", "production"]


class Settings(BaseSettings):
    """Every knob Sentinel reads, with its default and its constraint."""

    model_config = SettingsConfigDict(
        env_prefix="SENTINEL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    env: Environment = "local"

    # -- data stores --------------------------------------------------------------------

    database_url: PostgresDsn = Field(
        default=PostgresDsn("postgresql+psycopg://sentinel:sentinel_dev@localhost:5434/sentinel"),
        description="PostgreSQL DSN. Port 5434 locally to stay clear of other projects.",
    )
    redis_url: RedisDsn = Field(default=RedisDsn("redis://localhost:6381/0"))

    # -- object storage (SVC-02) --------------------------------------------------------

    s3_endpoint_url: str | None = Field(
        default="http://localhost:9010",
        description="MinIO locally; None in production so boto3 resolves real AWS S3.",
    )
    s3_access_key: str = "sentinel"
    # The local MinIO credential from docker-compose. Safe to keep in source: it grants
    # nothing beyond a throwaway container, and _reject_incoherent_configurations
    # refuses to start production with it still set.
    s3_secret_key: str = "sentinel_dev_secret"  # noqa: S105
    s3_bucket: str = "sentinel-documents"
    s3_region: str = "us-east-1"

    # -- ingestion (SVC-10) -------------------------------------------------------------

    max_document_bytes: int = Field(
        default=25 * 1024 * 1024,
        gt=0,
        description="Ceiling on an uploaded document. Bounds both memory and vision cost.",
    )

    # -- extraction (SVC-20) ------------------------------------------------------------

    extraction_provider: Literal["fixture", "anthropic"] = Field(
        default="fixture",
        description="'fixture' is the deterministic stand-in from ADR-0006; Q-1 is still open.",
    )
    extraction_min_confidence: Decimal = Field(
        default=Decimal("0.80"),
        ge=Decimal(0),
        le=Decimal(1),
        description="Below this, a field is not trusted and the payload is rejected (spec §4.2).",
    )
    anthropic_api_key: str | None = None

    # -- observability ------------------------------------------------------------------

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # -- validators ---------------------------------------------------------------------

    @field_validator("s3_endpoint_url")
    @classmethod
    def _blank_endpoint_means_real_s3(cls, value: str | None) -> str | None:
        """An empty string in the environment means "use AWS", not "use the empty host"."""
        return value or None

    @model_validator(mode="after")
    def _reject_incoherent_configurations(self) -> Settings:
        """Catch combinations that are individually valid and jointly wrong."""
        if self.extraction_provider == "anthropic" and not self.anthropic_api_key:
            raise ConfigurationError(
                "extraction_provider is 'anthropic' but no anthropic_api_key is set. "
                "Set SENTINEL_ANTHROPIC_API_KEY, or use the 'fixture' provider."
            )

        if self.env == "production":
            if self.extraction_provider == "fixture":
                raise ConfigurationError(
                    "the 'fixture' extraction provider returns canned data and must never "
                    "run in production -- it would post invoices that were never read."
                )
            if self.s3_secret_key == "sentinel_dev_secret":  # noqa: S105
                raise ConfigurationError(
                    "the development S3 credentials are still set in production."
                )

        return self

    # -- derived ------------------------------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """The process-wide settings, resolved once.

    Cached so that configuration cannot drift mid-run: two stages reading different values
    for the same tolerance would make a decision impossible to reconstruct. Tests that need
    different settings call ``get_settings.cache_clear()``.
    """
    return Settings()
