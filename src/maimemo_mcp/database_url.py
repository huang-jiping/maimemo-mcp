"""Secret-safe database URL parsing shared by runtime and maintenance commands."""

from __future__ import annotations

from sqlalchemy.engine import URL, make_url

_ALLOWED_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)


class DatabaseUrlError(ValueError):
    """A controlled URL validation error that never retains the supplied URL."""


def parse_database_url(raw: str, *, required_driver: str | None = None) -> URL:
    """Parse a database URL without retaining a parser exception on failure."""
    parsed: URL | None
    try:
        parsed = make_url(raw)
    except Exception:
        parsed = None

    # Raise outside the handler so __context__ cannot retain credentials from
    # SQLAlchemy's parsing exception.
    if parsed is None:
        raise DatabaseUrlError("Invalid database URL") from None
    return validate_database_url(parsed, required_driver=required_driver)


def validate_database_url(url: URL, *, required_driver: str | None = None) -> URL:
    """Validate the bounded connection options accepted by this project."""
    if required_driver is not None and url.drivername != required_driver:
        raise DatabaseUrlError(f"database_url must use {required_driver}") from None
    query_is_safe = all(
        key == "sslmode" and isinstance(value, str) and value in _ALLOWED_SSLMODES
        for key, value in url.query.items()
    )
    if not query_is_safe:
        raise DatabaseUrlError("Unsupported database URL query") from None
    return url
