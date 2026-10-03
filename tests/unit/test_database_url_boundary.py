"""Database engine construction remains secret-safe if model validation is bypassed."""

import traceback

import pytest
from maimemo.database_url import DatabaseUrlError
from maimemo.storage.database import create_async_engine


def test_engine_boundary_redacts_late_dialect_url_errors() -> None:
    database_url = (
        "postgresql+psycopg://u:prefix@host/d?port=REVIEW_SECRET@localhost/d"
    )

    with pytest.raises(DatabaseUrlError) as caught:
        create_async_engine(database_url)

    formatted = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "Invalid database URL"
    assert "REVIEW_SECRET" not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_engine_boundary_rejects_ambiguous_authority_if_settings_validation_is_bypassed() -> None:
    database_url = "postgresql+psycopg://u:prefix@REVIEW_SECRET@localhost/d"

    with pytest.raises(DatabaseUrlError) as caught:
        create_async_engine(database_url)

    formatted = "".join(traceback.format_exception(caught.value))
    assert "REVIEW_SECRET" not in formatted
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
