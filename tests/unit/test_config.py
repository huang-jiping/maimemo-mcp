from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from maimemo_mcp.config import Settings


@pytest.fixture
def environ(tmp_path: Path) -> dict[str, str]:
    return {
        "MAIMEMO_DATABASE_URL": "postgresql+psycopg://localhost/maimemo_test",
        "MAIMEMO_TOKEN_FILE": str(tmp_path / "token"),
        "MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE": str(tmp_path / "key"),
    }


def test_settings_require_token_file_not_plain_token(environ: dict[str, str]) -> None:
    del environ["MAIMEMO_TOKEN_FILE"]
    environ["MAIMEMO_TOKEN"] = "synthetic-token-not-a-secret"
    with pytest.raises(ValidationError):
        Settings.load(environ)


@pytest.mark.parametrize("invalid", [False, True])
def test_validation_errors_hide_all_supplied_secret_values(invalid: bool) -> None:
    import traceback
    private_url = "postgresql://u:LEAK@h/d"
    values = {"MAIMEMO_DATABASE_URL": private_url}
    if invalid:
        values.update({"MAIMEMO_TOKEN_FILE": "unused",
                       "MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE": "unused",
                       "MAIMEMO_MCP_PORT": private_url})
    with pytest.raises(ValidationError) as caught:
        Settings.load(values)
    formatted = "".join(traceback.format_exception(caught.value))
    assert "LEAK" not in formatted
    assert private_url not in str(caught.value)


def test_malformed_database_url_is_rejected_without_retaining_secret_context(
    environ: dict[str, str],
) -> None:
    import traceback

    private_url = "postgresql+psycopg://u:prefix@host:LEAK@localhost/d"
    environ["MAIMEMO_DATABASE_URL"] = private_url

    with pytest.raises(ValidationError) as caught:
        Settings.load(environ)

    formatted = "".join(traceback.format_exception(caught.value))
    assert "LEAK" not in formatted
    assert private_url not in formatted
    assert caught.value.errors(include_input=False, include_context=False, include_url=False) == [
        {
            "type": "value_error",
            "loc": ("database_url",),
            "msg": "Value error, Invalid database URL",
        }
    ]


def test_database_url_requires_async_psycopg_driver(environ: dict[str, str]) -> None:
    environ["MAIMEMO_DATABASE_URL"] = "postgresql://u:secret@localhost/d"

    with pytest.raises(ValidationError) as caught:
        Settings.load(environ)

    errors = caught.value.errors(include_input=False, include_context=False, include_url=False)
    assert errors == [
        {
            "type": "value_error",
            "loc": ("database_url",),
            "msg": "Value error, database_url must use postgresql+psycopg",
        }
    ]


def test_read_maimemo_token_strips_trailing_newline(environ: dict[str, str]) -> None:
    Path(environ["MAIMEMO_TOKEN_FILE"]).write_text("synthetic-token\r\n", encoding="utf-8")
    token = Settings.load(environ).read_maimemo_token()
    assert isinstance(token, SecretStr)
    assert token.get_secret_value() == "synthetic-token"


def test_read_token_fingerprint_key(environ: dict[str, str]) -> None:
    Path(environ["MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE"]).write_text(
        "synthetic-key\n", encoding="utf-8"
    )
    assert Settings.load(environ).read_token_fingerprint_key().get_secret_value() == "synthetic-key"


def test_secret_is_absent_from_settings_repr(environ: dict[str, str]) -> None:
    environ["MAIMEMO_DATABASE_URL"] = "postgresql+psycopg://test:synthetic-password@localhost/test"
    Path(environ["MAIMEMO_TOKEN_FILE"]).write_text("synthetic-token", encoding="utf-8")
    settings = Settings.load(environ)
    assert "synthetic-password" not in repr(settings)
    assert "synthetic-token" not in repr(settings)
    assert "synthetic-token" not in repr(settings.read_maimemo_token())


def test_load_does_not_read_secret_files(environ: dict[str, str]) -> None:
    settings = Settings.load(environ)
    with pytest.raises(FileNotFoundError):
        settings.read_maimemo_token()


@pytest.mark.parametrize("field", ["MAIMEMO_DATABASE_URL", "MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE"])
def test_required_configuration(environ: dict[str, str], field: str) -> None:
    del environ[field]
    with pytest.raises(ValidationError):
        Settings.load(environ)


def test_default_schedule_and_timezone(environ: dict[str, str]) -> None:
    settings = Settings.load(environ)
    assert settings.timezone.key == "Asia/Shanghai"
    assert settings.mcp_host == "0.0.0.0"
    assert settings.mcp_port == 8000
    assert settings.mcp_allowed_hosts == ()
    assert settings.today_interval_minutes == 30
    assert settings.records_interval_minutes == 120
    assert settings.log_level == "INFO"
    assert settings.openapi_drift_state_file == Path("var/openapi-drift.json")


def test_explicit_environment_overrides(environ: dict[str, str]) -> None:
    environ.update({
        "MAIMEMO_TIMEZONE": "UTC",
        "MAIMEMO_MCP_HOST": "127.0.0.1",
        "MAIMEMO_MCP_PORT": "9000",
        "MAIMEMO_MCP_ALLOWED_HOSTS": "maimemo-mcp, tunnel-sidecar",
        "MAIMEMO_TODAY_INTERVAL_MINUTES": "15",
        "MAIMEMO_RECORDS_INTERVAL_MINUTES": "60",
        "MAIMEMO_LOG_LEVEL": "DEBUG",
        "MAIMEMO_OPENAPI_DRIFT_STATE_FILE": "runtime/drift.json",
    })
    settings = Settings.load(environ)
    assert settings.timezone.key == "UTC"
    assert settings.mcp_host == "127.0.0.1"
    assert settings.mcp_port == 9000
    assert settings.mcp_allowed_hosts == ("maimemo-mcp", "tunnel-sidecar")
    assert settings.today_interval_minutes == 15
    assert settings.records_interval_minutes == 60
    assert settings.log_level == "DEBUG"
    assert settings.openapi_drift_state_file == Path("runtime/drift.json")


@pytest.mark.parametrize(("field", "value"), [
    ("MAIMEMO_DATABASE_URL", ""),
    ("MAIMEMO_TOKEN_FILE", ""),
    ("MAIMEMO_TIMEZONE", "Invalid/Zone"),
    ("MAIMEMO_MCP_PORT", "0"),
    ("MAIMEMO_MCP_PORT", "65536"),
    ("MAIMEMO_MCP_ALLOWED_HOSTS", "maimemo-mcp,*.internal"),
    ("MAIMEMO_MCP_ALLOWED_HOSTS", "maimemo-mcp:8000"),
    ("MAIMEMO_TODAY_INTERVAL_MINUTES", "0"),
    ("MAIMEMO_RECORDS_INTERVAL_MINUTES", "-1"),
    ("MAIMEMO_LOG_LEVEL", "invalid"),
    ("MAIMEMO_OPENAPI_DRIFT_STATE_FILE", ""),
])
def test_invalid_configuration(environ: dict[str, str], field: str, value: str) -> None:
    environ[field] = value
    with pytest.raises(ValidationError):
        Settings.load(environ)


@pytest.mark.parametrize("content", ["", "\n", " \r\n"])
def test_empty_secret_file_is_rejected(environ: dict[str, str], content: str) -> None:
    Path(environ["MAIMEMO_TOKEN_FILE"]).write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        Settings.load(environ).read_maimemo_token()


def test_load_reads_process_environment(
    environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    assert Settings.load().token_file == Path(environ["MAIMEMO_TOKEN_FILE"])


@pytest.mark.parametrize(("field", "reader", "secret"), [
    ("MAIMEMO_TOKEN_FILE", "read_maimemo_token", "synthetic-token"),
    ("MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE", "read_token_fingerprint_key", "synthetic-key"),
])
def test_invalid_utf8_secret_file_raises_redacted_error(
    environ: dict[str, str], field: str, reader: str, secret: str
) -> None:
    Path(environ[field]).write_bytes(secret.encode("utf-8") + b"\xff")
    settings = Settings.load(environ)

    with pytest.raises(ValueError) as caught:
        getattr(settings, reader)()

    error = caught.value
    assert secret not in repr(error)
    assert secret not in repr(error.args)
    assert secret not in repr(error.__cause__)
    assert secret not in repr(error.__context__)
    assert type(error) is ValueError
    assert str(error) == "Secret file must be valid UTF-8"
    assert error.args == ("Secret file must be valid UTF-8",)
    assert error.__cause__ is None
    assert error.__context__ is None
