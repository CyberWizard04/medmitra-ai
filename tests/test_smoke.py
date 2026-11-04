from datetime import datetime
from zoneinfo import ZoneInfo

from medmitra.config import APP_TIMEZONE, get_secret


def test_ist_morning_dose_converts_to_correct_utc():
    local = datetime(2026, 11, 5, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    utc = local.astimezone(ZoneInfo("UTC"))
    assert (utc.hour, utc.minute) == (2, 30)


def test_configured_timezone_is_valid():
    ZoneInfo(APP_TIMEZONE)  # raises if the name is wrong


def test_missing_secret_returns_none_without_crashing():
    assert get_secret("THIS_SECRET_DOES_NOT_EXIST") is None
