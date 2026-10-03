from pathlib import Path

import pytest
from maimemo.config import (
    AnalysisIntervals,
    CoreSettings,
    DatabaseSettings,
    UpstreamCredentialSettings,
)
from maimemo_mcp.config import MCPSettings as Settings
from maimemo_server.config import MigrationSettings
from maimemo_worker.config import WorkerSettings
from pydantic import SecretStr, ValidationError


def minimal_database_environment() -> dict[str, str]:
    return {"MAIMEMO_DATABASE_URL": "postgresql+psycopg://localhost/maimemo_test"}


def test_core_settings_do_not_define_module_ports_or_intervals() -> None:
    assert set(CoreSettings.model_fields) == {"database", "timezone", "log_level"}


def test_core_settings_load_without_upstream_credentials() -> None:
    settings = CoreSettings.load(minimal_database_environment())

    assert isinstance(settings.database, DatabaseSettings)
    assert settings.database.database_url.startswith("postgresql+psycopg://")


def test_upstream_credentials_are_loaded_only_when_requested(tmp_path: Path) -> None:
    environment = {
        "MAIMEMO_TOKEN_FILE": str(tmp_path / "token"),
        "MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE": str(tmp_path / "key"),
    }

    credentials = UpstreamCredentialSettings.load(environment)

    assert credentials.token_file == tmp_path / "token"
    assert credentials.token_fingerprint_key_file == tmp_path / "key"


def test_analysis_intervals_have_worker_defaults() -> None:
    intervals = AnalysisIntervals.load({})

    assert intervals.today_interval_minutes == 30
    assert intervals.records_interval_minutes == 120


def test_worker_settings_do_not_require_mcp_environment(environ: dict[str, str]) -> None:
    settings = WorkerSettings.load(environ)

    assert settings.intervals.today_interval_minutes == 30
    assert settings.intervals.records_interval_minutes == 120
    assert settings.core.database.database_url == environ["MAIMEMO_DATABASE_URL"]


def test_core_database_error_does_not_retain_secret() -> None:
    import traceback

    private_url = "postgresql+psycopg://u:prefix@host:CORE_SECRET@localhost/d"
    with pytest.raises(ValidationError) as caught:
        CoreSettings.load({"MAIMEMO_DATABASE_URL": private_url})

    formatted = "".join(traceback.format_exception(caught.value))
    assert "CORE_SECRET" not in formatted
    assert private_url not in formatted


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


@pytest.mark.parametrize(
    "query",
    [
        *(f"{key}=REVIEW_SECRET" for key in (
            "host", "hostaddr", "port", "user", "password", "passfile", "dbname",
            "service", "servicefile", "sslpassword",
        )),
        "PORT=REVIEW_SECRET",
        "host=first&host=REVIEW_SECRET",
        "application_name=REVIEW_SECRET",
        "sslmode=REVIEW_SECRET",
        "sslmode=require&sslmode=REVIEW_SECRET",
        "",
        "host=",
        "sslmode=",
        "sslmode=require&sslmode=",
        "sslmode=require&",
        "%73slmode=require",
        "sslmode=%72equire",
        "sslmode=require;port=5432",
    ],
)
def test_database_url_query_is_fail_closed_without_retaining_values(
    environ: dict[str, str], query: str,
) -> None:
    import traceback

    environ["MAIMEMO_DATABASE_URL"] = f"postgresql+psycopg://u:p@localhost/d?{query}"

    with pytest.raises(ValidationError) as caught:
        Settings.load(environ)

    formatted = "".join(traceback.format_exception(caught.value))
    assert "REVIEW_SECRET" not in formatted
    assert caught.value.errors(include_input=False, include_context=False, include_url=False) == [
        {
            "type": "value_error",
            "loc": ("database_url",),
            "msg": "Value error, Unsupported database URL query",
        }
    ]


def test_database_url_allows_bounded_sslmode(environ: dict[str, str]) -> None:
    environ["MAIMEMO_DATABASE_URL"] += "?sslmode=require"
    assert Settings.load(environ).core.database.database_url.endswith("?sslmode=require")


@pytest.mark.parametrize("private_url", [
    "postgresql+psycopg://u:prefix@REVIEW_SECRET@localhost/d",
    "postgresql+psycopg://u:p@first@REVIEW_SECRET/d",
    "postgresql+psycopg://u:p@REVIEW%40SECRET/d",
    "postgresql+psycopg://u:p@REVIEW SECRET/d",
    "postgresql+psycopg://u:p@REVIEW%20SECRET/d",
    "postgresql+psycopg://u:p%20REVIEW_SECRET@localhost/d",
    "postgresql+psycopg://u:p%0AREVIEW_SECRET@localhost/d",
    "postgresql+psycopg://u:p@localhost\\REVIEW_SECRET/d",
    "postgresql+psycopg://u:p@[::1/d",
    "postgresql+psycopg://u:p@localhost:abc/d",
    "postgresql+psycopg://u:p@localhost:99999/d",
    "postgresql+psycopg://u:prefix/REVIEW_SECRET@localhost/d",
    "postgresql+psycopg://u:p@localhost/d#REVIEW_SECRET",
    "postgresql+psycopg://u:p@localhost/d%ZZ",
])
def test_raw_database_authority_and_path_ambiguity_is_rejected_without_values(
    environ: dict[str, str], private_url: str,
) -> None:
    import traceback

    environ["MAIMEMO_DATABASE_URL"] = private_url
    with pytest.raises(ValidationError) as caught:
        Settings.load(environ)

    formatted = "".join(traceback.format_exception(caught.value))
    assert "REVIEW_SECRET" not in formatted
    assert private_url not in formatted
    assert caught.value.errors(include_input=False, include_context=False, include_url=False) == [
        {
            "type": "value_error",
            "loc": ("database_url",),
            "msg": "Value error, Invalid database URL",
        }
    ]


@pytest.mark.parametrize("sslmode", [
    "disable", "allow", "prefer", "require", "verify-ca", "verify-full",
])
def test_raw_database_url_accepts_encoded_password_ipv6_and_sslmode(
    environ: dict[str, str], sslmode: str,
) -> None:
    environ["MAIMEMO_DATABASE_URL"] = (
        "postgresql+psycopg://u:p%40ss%3Aword%2Fpercent%25@[::1]:5432/d"
        f"?sslmode={sslmode}"
    )
    assert Settings.load(environ).core.database.database_url == environ["MAIMEMO_DATABASE_URL"]


@pytest.mark.parametrize("url", [
    "postgresql+psycopg://localhost/d",
    "postgresql+psycopg://unused",
])
def test_raw_database_url_accepts_safe_host_without_userinfo_or_database(
    environ: dict[str, str], url: str,
) -> None:
    environ["MAIMEMO_DATABASE_URL"] = url
    assert Settings.load(environ).core.database.database_url == url


def test_read_maimemo_token_strips_trailing_newline(environ: dict[str, str]) -> None:
    Path(environ["MAIMEMO_TOKEN_FILE"]).write_text("synthetic-token\r\n", encoding="utf-8")
    token = Settings.load(environ).upstream.read_maimemo_token()
    assert isinstance(token, SecretStr)
    assert token.get_secret_value() == "synthetic-token"


def test_read_token_fingerprint_key(environ: dict[str, str]) -> None:
    Path(environ["MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE"]).write_text(
        "synthetic-key\n", encoding="utf-8"
    )
    assert (
        Settings.load(environ).upstream.read_token_fingerprint_key().get_secret_value()
        == "synthetic-key"
    )


def test_secret_is_absent_from_settings_repr(environ: dict[str, str]) -> None:
    environ["MAIMEMO_DATABASE_URL"] = "postgresql+psycopg://test:synthetic-password@localhost/test"
    Path(environ["MAIMEMO_TOKEN_FILE"]).write_text("synthetic-token", encoding="utf-8")
    settings = Settings.load(environ)
    assert "synthetic-password" not in repr(settings)
    assert "synthetic-token" not in repr(settings)
    assert "synthetic-token" not in repr(settings.upstream.read_maimemo_token())


def test_load_does_not_read_secret_files(environ: dict[str, str]) -> None:
    settings = Settings.load(environ)
    with pytest.raises(FileNotFoundError):
        settings.upstream.read_maimemo_token()


@pytest.mark.parametrize("field", ["MAIMEMO_DATABASE_URL", "MAIMEMO_TOKEN_FINGERPRINT_KEY_FILE"])
def test_required_configuration(environ: dict[str, str], field: str) -> None:
    del environ[field]
    with pytest.raises(ValidationError):
        Settings.load(environ)


def test_default_schedule_and_timezone(environ: dict[str, str]) -> None:
    settings = Settings.load(environ)
    assert settings.core.timezone.key == "Asia/Shanghai"
    assert settings.host == "0.0.0.0"
    assert settings.port == 8000
    assert settings.allowed_hosts == ()
    assert settings.core.log_level == "INFO"
    assert settings.drift_state_file == Path("var/openapi-drift.json")


def test_startup_timeout_defaults(environ: dict[str, str]) -> None:
    assert Settings.load(environ).core.database.connect_timeout_seconds == 10
    migration = MigrationSettings.load(environ)
    assert migration.lock_timeout_seconds == 30
    assert migration.statement_timeout_seconds == 300
    assert WorkerSettings.load(environ).schema_wait_timeout_seconds == 60


@pytest.mark.parametrize(
    ("environment_name", "loader", "path", "maximum"),
    [
        (
            "MAIMEMO_DATABASE_CONNECT_TIMEOUT_SECONDS",
            Settings.load,
            lambda value: value.core.database.connect_timeout_seconds,
            60,
        ),
        (
            "MAIMEMO_MIGRATION_LOCK_TIMEOUT_SECONDS",
            MigrationSettings.load,
            lambda value: value.lock_timeout_seconds,
            300,
        ),
        (
            "MAIMEMO_MIGRATION_STATEMENT_TIMEOUT_SECONDS",
            MigrationSettings.load,
            lambda value: value.statement_timeout_seconds,
            3600,
        ),
        (
            "MAIMEMO_SCHEMA_WAIT_TIMEOUT_SECONDS",
            WorkerSettings.load,
            lambda value: value.schema_wait_timeout_seconds,
            600,
        ),
    ],
)
def test_startup_timeouts_have_positive_bounded_environment_values(
    environ: dict[str, str],
    environment_name: str,
    loader: object,
    path: object,
    maximum: int,
) -> None:
    for value in (0, -1, maximum + 1):
        environ[environment_name] = str(value)
        with pytest.raises(ValidationError):
            loader(environ)  # type: ignore[operator]
    for value in (1, maximum):
        environ[environment_name] = str(value)
        assert path(loader(environ)) == value  # type: ignore[operator]


def test_unreadable_secret_error_does_not_retain_private_path(
    environ: dict[str, str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback

    def denied(path: Path, **kwargs: object) -> str:
        raise PermissionError("SYNTHETIC_PATH_PASSWORD")

    monkeypatch.setattr(Path, "read_text", denied)
    with pytest.raises(OSError) as caught:
        Settings.load(environ).upstream.read_maimemo_token()
    assert "SYNTHETIC_PATH_PASSWORD" not in "".join(traceback.format_exception(caught.value))
    assert caught.value.__context__ is None


def test_explicit_environment_overrides(environ: dict[str, str]) -> None:
    environ.update({
        "MAIMEMO_TIMEZONE": "UTC",
        "MAIMEMO_MCP_HOST": "127.0.0.1",
        "MAIMEMO_MCP_PORT": "9000",
        "MAIMEMO_MCP_ALLOWED_HOSTS": "maimemo-mcp, tunnel-sidecar",
        "MAIMEMO_LOG_LEVEL": "DEBUG",
        "MAIMEMO_OPENAPI_DRIFT_STATE_FILE": "runtime/drift.json",
    })
    settings = Settings.load(environ)
    assert settings.core.timezone.key == "UTC"
    assert settings.host == "127.0.0.1"
    assert settings.port == 9000
    assert settings.allowed_hosts == ("maimemo-mcp", "tunnel-sidecar")
    assert settings.core.log_level == "DEBUG"
    assert settings.drift_state_file == Path("runtime/drift.json")


@pytest.mark.parametrize(("field", "value"), [
    ("MAIMEMO_DATABASE_URL", ""),
    ("MAIMEMO_TOKEN_FILE", ""),
    ("MAIMEMO_TIMEZONE", "Invalid/Zone"),
    ("MAIMEMO_MCP_PORT", "0"),
    ("MAIMEMO_MCP_PORT", "65536"),
    ("MAIMEMO_MCP_ALLOWED_HOSTS", "maimemo-mcp,*.internal"),
    ("MAIMEMO_MCP_ALLOWED_HOSTS", "maimemo-mcp:8000"),
    ("MAIMEMO_LOG_LEVEL", "invalid"),
    ("MAIMEMO_OPENAPI_DRIFT_STATE_FILE", ""),
])
def test_invalid_configuration(environ: dict[str, str], field: str, value: str) -> None:
    environ[field] = value
    with pytest.raises(ValidationError):
        Settings.load(environ)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("MAIMEMO_TODAY_INTERVAL_MINUTES", "0"),
        ("MAIMEMO_RECORDS_INTERVAL_MINUTES", "-1"),
    ],
)
def test_invalid_analysis_interval_configuration(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        AnalysisIntervals.load({field: value})


@pytest.mark.parametrize("content", ["", "\n", " \r\n"])
def test_empty_secret_file_is_rejected(environ: dict[str, str], content: str) -> None:
    Path(environ["MAIMEMO_TOKEN_FILE"]).write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        Settings.load(environ).upstream.read_maimemo_token()


def test_load_reads_process_environment(
    environ: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    assert Settings.load().upstream.token_file == Path(environ["MAIMEMO_TOKEN_FILE"])


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
        getattr(settings.upstream, reader)()

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
