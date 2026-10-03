"""Server configuration owns only database and public HTTP settings."""

import pytest
from maimemo.config import DatabaseSettings
from maimemo_server.config import ServerSettings
from pydantic import ValidationError


@pytest.fixture
def environ() -> dict[str, str]:
    return {"MAIMEMO_DATABASE_URL": "postgresql+psycopg://u:p@db.invalid:5432/maimemo"}


def test_server_loads_without_upstream_secrets(environ: dict[str, str]) -> None:
    settings = ServerSettings.load(environ)

    assert settings.port == 8080
    assert settings.database.database_url == environ["MAIMEMO_DATABASE_URL"]


def test_server_rejects_non_https_external_base_url() -> None:
    with pytest.raises(ValidationError):
        ServerSettings(
            database=DatabaseSettings(
                database_url="postgresql+psycopg://u:p@db.invalid:5432/maimemo"
            ),
            external_base_url="http://maimemo.example",
        )


def test_server_loads_http_overrides(environ: dict[str, str]) -> None:
    environ.update(
        {
            "MAIMEMO_SERVER_HOST": "127.0.0.1",
            "MAIMEMO_SERVER_PORT": "9090",
            "MAIMEMO_EXTERNAL_BASE_URL": "https://maimemo.huangjiping.com",
        }
    )

    settings = ServerSettings.load(environ)

    assert settings.host == "127.0.0.1"
    assert settings.port == 9090
    assert str(settings.external_base_url) == "https://maimemo.huangjiping.com/"
