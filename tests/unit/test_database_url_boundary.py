"""Database engine construction remains secret-safe if model validation is bypassed."""

import traceback
from pathlib import Path

import pytest

from maimemo_mcp.config import Settings
from maimemo_mcp.database_url import DatabaseUrlError
from maimemo_mcp.storage.database import create_async_engine_from_settings


def test_engine_boundary_redacts_late_dialect_url_errors() -> None:
    settings = Settings.model_construct(
        database_url=(
            "postgresql+psycopg://u:prefix@host/d?port=REVIEW_SECRET@localhost/d"
        ),
        token_file=Path("unused"),
        token_fingerprint_key_file=Path("unused"),
    )

    with pytest.raises(DatabaseUrlError) as caught:
        create_async_engine_from_settings(settings)

    formatted = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "Invalid database URL"
    assert "REVIEW_SECRET" not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_engine_boundary_rejects_ambiguous_authority_if_settings_validation_is_bypassed() -> None:
    settings = Settings.model_construct(
        database_url="postgresql+psycopg://u:prefix@REVIEW_SECRET@localhost/d",
        token_file=Path("unused"),
        token_fingerprint_key_file=Path("unused"),
    )

    with pytest.raises(DatabaseUrlError) as caught:
        create_async_engine_from_settings(settings)

    formatted = "".join(traceback.format_exception(caught.value))
    assert "REVIEW_SECRET" not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
