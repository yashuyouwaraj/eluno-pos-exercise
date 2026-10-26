"""Business-calendar helpers for Bengaluru store operations."""

from datetime import date, datetime, timedelta, timezone

BENGALURU_TZ = timezone(timedelta(hours=5, minutes=30))


def bengaluru_business_date(value: str) -> date:
    timestamp = str(value)
    timestamp = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    parsed = datetime.fromisoformat(timestamp)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BENGALURU_TZ)
    return parsed.astimezone(BENGALURU_TZ).date()


def bengaluru_today() -> date:
    return datetime.now(BENGALURU_TZ).date()
