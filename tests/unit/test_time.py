from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from maimemo.time import learning_date


@pytest.mark.parametrize(("hour", "minute", "expected"), [
    (15, 59, date(2026, 10, 2)),
    (16, 0, date(2026, 10, 3)),
])
def test_learning_date_uses_asia_shanghai_at_utc_boundary(
    hour: int, minute: int, expected: date
) -> None:
    at = datetime(2026, 10, 2, hour, minute, tzinfo=UTC)
    assert learning_date(at, ZoneInfo("Asia/Shanghai")) == expected


def test_learning_date_respects_supplied_timezone() -> None:
    at = datetime(2026, 10, 2, 16, tzinfo=UTC)
    assert learning_date(at, ZoneInfo("UTC")) == date(2026, 10, 2)


def test_learning_date_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        learning_date(datetime(2026, 10, 2), ZoneInfo("Asia/Shanghai"))
