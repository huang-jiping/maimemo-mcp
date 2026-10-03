"""Secret-safe database URL parsing shared by runtime and maintenance commands."""

from __future__ import annotations

import ipaddress
import re
from typing import Literal
from urllib.parse import unquote_to_bytes

from sqlalchemy.engine import URL, make_url

_ALLOWED_SSLMODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")
_DNS_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*"
)
_DATABASE = re.compile(r"^[A-Za-z0-9_.-]+$")
_USERINFO = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:%-]+$")
_HEX = frozenset("0123456789abcdefABCDEF")


class DatabaseUrlError(ValueError):
    """A controlled URL validation error that never retains the supplied URL."""


def parse_database_url(raw: str, *, required_driver: str | None = None) -> URL:
    """Parse a database URL without retaining a parser exception on failure."""
    raw_error = _raw_url_error(raw)
    if raw_error is not None:
        message = (
            "Unsupported database URL query"
            if raw_error == "query"
            else "Invalid database URL"
        )
        raise DatabaseUrlError(message) from None

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
    fields_are_safe = False
    try:
        fields_are_safe = (
            url.host is not None
            and _valid_parsed_host(url.host)
            and _valid_port(url.port)
            and (url.database is None or bool(_DATABASE.fullmatch(url.database)))
        )
    except Exception:
        fields_are_safe = False
    if not fields_are_safe:
        raise DatabaseUrlError("Invalid database URL") from None

    query_is_safe = _valid_query_items(url)
    if not query_is_safe:
        raise DatabaseUrlError("Unsupported database URL query") from None
    return url


def _raw_url_error(raw: str) -> Literal["url", "query"] | None:
    if (
        not raw
        or "\\" in raw
        or "#" in raw
        or any(ord(character) < 0x20 or ord(character) == 0x7F or character.isspace()
               for character in raw)
        or not _valid_percent_escapes(raw)
    ):
        return "url"

    scheme, separator, remainder = raw.partition("://")
    if not separator or not _SCHEME.fullmatch(scheme):
        return "url"

    query_marker = remainder.find("?")
    before_query = remainder if query_marker < 0 else remainder[:query_marker]
    raw_query = None if query_marker < 0 else remainder[query_marker + 1:]
    if raw_query is not None and not _valid_raw_query(raw_query):
        return "query"

    slash = before_query.find("/")
    authority = before_query if slash < 0 else before_query[:slash]
    path = "" if slash < 0 else before_query[slash:]
    if not authority or authority.count("@") > 1 or not _valid_raw_path(path):
        return "url"

    if "@" in authority:
        userinfo, host_port = authority.split("@", 1)
        if not _valid_userinfo(userinfo):
            return "url"
    else:
        host_port = authority
    if not _valid_raw_host_port(host_port):
        return "url"
    return None


def _valid_percent_escapes(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or value[index + 1] not in _HEX or value[index + 2] not in _HEX:
            return False
        index += 3
    return True


def _valid_userinfo(value: str) -> bool:
    if not value or not _USERINFO.fullmatch(value):
        return False
    decoded = unquote_to_bytes(value)
    return not any(byte <= 0x20 or byte == 0x7F for byte in decoded)


def _valid_raw_host_port(value: str) -> bool:
    if not value or "%" in value or "@" in value:
        return False
    if value.startswith("["):
        closing = value.find("]")
        if closing < 0:
            return False
        host = value[1:closing]
        remainder = value[closing + 1:]
        if remainder and not remainder.startswith(":"):
            return False
        port = None if not remainder else remainder[1:]
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
        return _valid_raw_port(port)
    if "[" in value or "]" in value or value.count(":") > 1:
        return False
    host, separator, port = value.rpartition(":")
    if not separator:
        host, port = value, None
    return _valid_dns_or_ipv4(host) and _valid_raw_port(port)


def _valid_dns_or_ipv4(host: str) -> bool:
    if not host:
        return False
    if all(character in "0123456789." for character in host) and "." in host:
        try:
            ipaddress.IPv4Address(host)
        except ValueError:
            return False
        return True
    return bool(_DNS_HOST.fullmatch(host))


def _valid_raw_port(port: str | None) -> bool:
    if port is None:
        return True
    return port.isascii() and port.isdigit() and 1 <= int(port) <= 65535


def _valid_parsed_host(host: str) -> bool:
    has_forbidden_character = any(character in host for character in "@/\\")
    if has_forbidden_character or any(character.isspace() for character in host):
        return False
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return False
        return True
    return _valid_dns_or_ipv4(host)


def _valid_port(port: int | None) -> bool:
    return port is None or 1 <= port <= 65535


def _valid_raw_path(path: str) -> bool:
    return path in {"", "/"} or (path.startswith("/") and bool(_DATABASE.fullmatch(path[1:])))


def _valid_raw_query(query: str) -> bool:
    if query.count("=") != 1:
        return False
    key, value = query.split("=", 1)
    return key == "sslmode" and value in _ALLOWED_SSLMODES


def _valid_query_items(url: URL) -> bool:
    return all(
        key == "sslmode" and isinstance(value, str) and value in _ALLOWED_SSLMODES
        for key, value in url.query.items()
    )
