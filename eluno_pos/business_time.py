"""Business-calendar helpers for Bengaluru store operations."""

from datetime import date, datetime, timedelta, timezone, tzinfo
import os

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:
    ZoneInfo = None

    class ZoneInfoNotFoundError(Exception):
        pass

BENGALURU_TZ = timezone(timedelta(hours=5, minutes=30))
DEFAULT_BUSINESS_TIMEZONE = "Asia/Kolkata"


def business_timezone_name() -> str:
    return os.environ.get("ELUNO_BUSINESS_TIMEZONE", DEFAULT_BUSINESS_TIMEZONE).strip()


def business_timezone() -> tzinfo:
    name = business_timezone_name()
    if name == DEFAULT_BUSINESS_TIMEZONE:
        return BENGALURU_TZ
    if name in ("UTC", "Etc/UTC", "GMT"):
        return timezone.utc
    if ZoneInfo is None:
        raise ValueError(
            "This Python runtime cannot load named timezone data; use the default Asia/Kolkata or UTC."
        )
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            "ELUNO_BUSINESS_TIMEZONE must name a timezone available to this Python runtime."
        ) from exc


def bengaluru_business_date(value: str) -> date:
    timestamp = str(value)
    timestamp = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    parsed = datetime.fromisoformat(timestamp)
    zone = business_timezone()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(zone).date()


def bengaluru_today() -> date:
    return datetime.now(business_timezone()).date()
