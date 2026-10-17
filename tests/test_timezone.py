"""Tests for timezone-aware scheduling.

Covers cross-timezone overlap blocking, storing bookings in the calendar's home
timezone, invalid timezone handling, and DST correctness.

Run: python -m pytest -q    (from the project root)
"""

import json
from datetime import date, timedelta
from zoneinfo import ZoneInfo

import pytest

from calendar_store import _event_interval, book_slot, check_calendar, load


@pytest.fixture
def cal(tmp_path):
    """A fresh calendar file for each test (home timezone UTC by default)."""
    return tmp_path / "calendar.json"


def _write_cal(path, timezone, events, working_hours=None):
    data = {
        "owner": "you",
        "timezone": timezone,
        "working_hours": working_hours or {"start": "09:00", "end": "17:00"},
        "slot_minutes": 30,
        "events": events,
    }
    path.write_text(json.dumps(data), encoding="utf-8")


def _next_weekday(offset_from_monday=0):
    today = date.today()
    days_ahead = (7 - today.weekday()) % 7 or 7
    return today + timedelta(days=days_ahead + offset_from_monday)


def test_cross_timezone_overlap_is_blocked(cal):
    """An event in one zone blocks a booking at the same instant in another."""
    day = _next_weekday(1)  # Tuesday, home timezone UTC
    _write_cal(
        cal,
        "UTC",
        [
            {
                "id": "evt-1",
                "person": "Team",
                "date": day.isoformat(),
                "start": "09:00",
                "end": "10:00",
                "title": "Sync",
            }
        ],
    )

    # 05:00 New York (EDT, UTC-4) == 09:00 UTC -> same instant, must be refused.
    clash = book_slot("Sam", day.isoformat(), "05:00", timezone="America/New_York", path=cal)
    assert clash["ok"] is False
    assert "double-booking" in clash["error"]
    assert clash["conflict"]["person"] == "Team"


def test_booking_in_client_zone_is_stored_in_home_zone(cal):
    """A booking requested in another zone is stored as the calendar's wall clock."""
    day = _next_weekday(1)
    _write_cal(cal, "Asia/Kolkata", [])  # home = UTC+5:30

    # 05:00 UTC == 10:30 IST, inside 09:00-17:00 home hours.
    result = book_slot("Sam", day.isoformat(), "05:00", timezone="UTC", path=cal)
    assert result["ok"] is True
    assert result["booked"]["start"] == "10:30"
    assert result["booked"]["end"] == "11:00"
    assert result["booked"]["timezone"] == "Asia/Kolkata"
    assert "requested_time" in result

    saved = load(cal)["events"][0]
    assert saved["start"] == "10:30"
    assert saved["timezone"] == "Asia/Kolkata"


def test_booking_outside_home_hours_via_other_zone_is_rejected(cal):
    """Working hours are enforced in the home timezone, not the caller's."""
    day = _next_weekday(1)
    _write_cal(cal, "Asia/Kolkata", [])
    # 09:00 New York == 18:30 IST -> outside 09:00-17:00 home hours.
    result = book_slot("Sam", day.isoformat(), "09:00", timezone="America/New_York", path=cal)
    assert result["ok"] is False
    assert "working hours" in result["error"]


def test_check_calendar_renders_in_requested_zone(cal):
    day = _next_weekday(1)
    _write_cal(
        cal,
        "UTC",
        [
            {
                "id": "evt-1",
                "person": "Team",
                "date": day.isoformat(),
                "start": "09:00",
                "end": "10:00",
            }
        ],
    )
    result = check_calendar(day.isoformat(), day.isoformat(), timezone="Asia/Kolkata", path=cal)
    assert result["ok"] is True
    assert result["timezone"] == "Asia/Kolkata"
    assert result["home_timezone"] == "UTC"
    busy = result["days"][0]["busy"][0]
    assert busy["start"] == "14:30"  # 09:00 UTC -> 14:30 IST
    assert busy["end"] == "15:30"


def test_invalid_timezone_is_reported(cal):
    day = _next_weekday(1).isoformat()
    booking = book_slot("Sam", day, "10:00", timezone="Mars/Phobos", path=cal)
    assert booking["ok"] is False
    assert "unknown timezone" in booking["error"]

    check = check_calendar(day, day, timezone="Nope/Nope", path=cal)
    assert check["ok"] is False
    assert "unknown timezone" in check["error"]


def test_event_interval_respects_dst(cal):
    """The same wall-clock in New York maps to different UTC offsets by season."""
    summer = {
        "date": "2026-07-15",
        "start": "09:00",
        "end": "10:00",
        "timezone": "America/New_York",
    }
    winter = {
        "date": "2026-12-15",
        "start": "09:00",
        "end": "10:00",
        "timezone": "America/New_York",
    }
    home = ZoneInfo("UTC")

    start_summer, _ = _event_interval(summer, home)
    start_winter, _ = _event_interval(winter, home)
    assert start_summer.astimezone(home).strftime("%H:%M") == "13:00"  # EDT = UTC-4
    assert start_winter.astimezone(home).strftime("%H:%M") == "14:00"  # EST = UTC-5


def test_home_timezone_defaults_to_utc_when_invalid(cal):
    """A garbage home timezone falls back to UTC instead of crashing."""
    day = _next_weekday(1)
    _write_cal(cal, "not/a/zone", [])
    result = book_slot("Sam", day.isoformat(), "10:00", path=cal)
    assert result["ok"] is True
    assert result["booked"]["timezone"] == "UTC"
