"""Learning-day conversion from explicit instants."""

from datetime import date, datetime
from zoneinfo import ZoneInfo


def learning_date(at: datetime, zone: ZoneInfo) -> date:
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("Learning-day conversion requires a timezone-aware datetime")
    return at.astimezone(zone).date()
