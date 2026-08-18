"""Configuration must fail at startup, not mid-invoice."""

from __future__ import annotations

import pytest

from sentinel.core.errors import ConfigurationError
from sentinel.core.settings import Settings, get_settings


def build(**overrides: object) -> Settings:
    """A Settings instance built from explicit values, ignoring any ambient .env."""
    defaults: dict[str, object] = {"env": "local", "_env_file": None}
    return Settings(**{**defaults, **overrides})  # type: ignore[arg-type]


class TestDefaults:
    def test_local_defaults_are_coherent(self) -> None:
        settings = build()
        assert settings.env == "local"
        assert settings.extraction_provider == "fixture"
        assert settings.extraction_confidence_policy == "v1"
        assert not settings.is_production

    def test_default_ports_avoid_the_other_project_on_this_machine(self) -> None:
        settings = build()
        assert "5434" in str(settings.database_url)
        assert "6381" in str(settings.redis_url)
        assert settings.s3_endpoint_url is not None
        assert "9010" in settings.s3_endpoint_url

    def test_is_frozen(self) -> None:
        settings = build()
        with pytest.raises(Exception):  # noqa: B017 -- pydantic raises ValidationError
            settings.env = "production"  # type: ignore[misc]


class TestConstraints:
    @pytest.mark.parametrize("bad", [0, -1])
    def test_document_size_ceiling_must_be_positive(self, bad: int) -> None:
        with pytest.raises(ValueError):
            build(max_document_bytes=bad)

    def test_blank_s3_endpoint_means_real_aws(self) -> None:
        """An empty env var must resolve to None, not to an unusable empty host."""
        assert build(s3_endpoint_url="").s3_endpoint_url is None


class TestIncoherentCombinations:
    def test_anthropic_provider_without_a_key_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="no anthropic_api_key"):
            build(extraction_provider="anthropic", anthropic_api_key=None)

    def test_anthropic_provider_with_a_key_is_accepted(self) -> None:
        settings = build(extraction_provider="anthropic", anthropic_api_key="sk-test")
        assert settings.extraction_provider == "anthropic"

    def test_fixture_extractor_is_refused_in_production(self) -> None:
        """The fixture extractor returns canned data.

        In production it would post invoices whose contents were never actually read --
        the single most dangerous misconfiguration this system has.
        """
        with pytest.raises(ConfigurationError, match="never"):
            build(
                env="production",
                extraction_provider="fixture",
                s3_secret_key="real-secret",
            )

    def test_development_credentials_are_refused_in_production(self) -> None:
        with pytest.raises(ConfigurationError, match="development S3 credentials"):
            build(
                env="production",
                extraction_provider="anthropic",
                anthropic_api_key="sk-test",
                s3_secret_key="sentinel_dev_secret",
            )

    def test_a_coherent_production_configuration_is_accepted(self) -> None:
        settings = build(
            env="production",
            extraction_provider="anthropic",
            anthropic_api_key="sk-test",
            s3_secret_key="a-real-secret",
            s3_endpoint_url="",
        )
        assert settings.is_production
        assert settings.s3_endpoint_url is None


class TestCaching:
    def test_settings_are_resolved_once(self) -> None:
        """Two stages reading different values would make a decision unreproducible."""
        get_settings.cache_clear()
        try:
            assert get_settings() is get_settings()
        finally:
            get_settings.cache_clear()
