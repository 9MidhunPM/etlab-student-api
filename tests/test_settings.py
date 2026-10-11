import pytest
from pydantic import ValidationError

from etlab_api.settings import Settings


def settings(**kwargs: object) -> Settings:
    return Settings(_env_file=None, app_mode="multiuser", api_token="x" * 43, **kwargs)


def test_csv_values_and_defaults() -> None:
    s = settings(etlab_allowed_hosts="*.etlab.in,portal.college.example", cors_origins="")
    assert s.etlab_allowed_hosts == ("*.etlab.in", "portal.college.example")
    assert s.cache_ttl_seconds == 60
    assert s.cors_origins == ()


def test_configuration_errors_hide_secrets() -> None:
    with pytest.raises(ValidationError) as error:
        Settings(
            _env_file=None,
            app_mode="personal",
            api_token="do-not-disclose-this-secret",
            etlab_password="test-password-secret",
            etlab_username="student",
        )
    assert "do-not-disclose" not in str(error.value)
    assert "test-password-secret" not in str(error.value)


@pytest.mark.parametrize("value", [-1, 0])
def test_invalid_size(value: int) -> None:
    with pytest.raises(ValidationError):
        settings(max_download_bytes=value)
