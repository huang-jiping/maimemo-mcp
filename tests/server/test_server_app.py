"""The minimal Server exposes fixed safe routes before OAuth is implemented."""

import pytest
from maimemo.config import DatabaseSettings
from maimemo_server.app import create_app
from maimemo_server.config import ServerSettings
from starlette.testclient import TestClient


def settings() -> ServerSettings:
    return ServerSettings(
        database=DatabaseSettings(database_url="postgresql+psycopg://u:p@127.0.0.1:1/unused")
    )


def test_minimal_routes_and_oauth_fail_closed() -> None:
    private = "SYNTHETIC_AUTHORIZATION_CODE"
    with TestClient(create_app(settings())) as client:
        assert client.get("/").status_code == 200
        assert client.get("/health/live").json() == {"status": "live"}
        start = client.get("/oauth/start", params={"state": private})
        callback = client.get("/oauth/callback", params={"code": private})

    assert start.status_code == 503
    assert start.json() == {"error": "oauth_not_configured"}
    assert callback.status_code == 503
    assert callback.json() == {"error": "oauth_not_configured"}
    assert private not in start.text + callback.text


def test_readiness_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    class FailedConnection:
        async def __aenter__(self) -> None:
            raise RuntimeError("SYNTHETIC_DATABASE_PRIVATE")

        async def __aexit__(self, *args: object) -> None:
            return None

    class FailedEngine:
        def connect(self) -> FailedConnection:
            return FailedConnection()

        async def dispose(self) -> None:
            return None

    monkeypatch.setattr(
        "maimemo_server.app.create_async_engine",
        lambda database_url, **kwargs: FailedEngine(),
    )
    with TestClient(create_app(settings())) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "postgresql" not in response.text
    assert "SYNTHETIC_DATABASE_PRIVATE" not in response.text
