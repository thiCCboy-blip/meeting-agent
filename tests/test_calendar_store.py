"""Tests for the fake calendar store, with a focus on the no-double-book rule.

Run: python -m pytest -q    (from the project root)
"""

import json
from datetime import date, timedelta

import pytest

from calendar_store import book_slot, check_calendar, free_slots, load


@pytest.fixture
def cal(tmp_path):
    """A fresh, empty calendar file for each test."""
    return tmp_path / "calendar.json"


def _next_weekday(offset_from_monday=0):
    """A date on the next Monday..Friday relative to today."""
    today = date.today()
    days_ahead = (7 - today.weekday()) % 7 or 7  # next Monday
    return today + timedelta(days=days_ahead + offset_from_monday)


def test_booking_a_free_slot_succeeds_and_persists(cal):
    day = _next_weekday(1)  # Tuesday
    result = book_slot("Sam", day.isoformat(), "11:00", path=cal)
    assert result["ok"] is True
    assert result["booked"]["start"] == "11:00"
    assert result["booked"]["end"] == "11:30"

    saved = json.loads(cal.read_text(encoding="utf-8"))
    assert len(saved["events"]) == 1
    assert saved["events"][0]["person"] == "Sam"


def test_double_booking_is_blocked(cal):
    day = _next_weekday(1).isoformat()
    first = book_slot("Sam", day, "11:00", path=cal)
    assert first["ok"] is True

    # Exact same slot must be refused.
    clash = book_slot("Priya", day, "11:00", path=cal)
    assert clash["ok"] is False
    assert "double-booking" in clash["error"]
    assert clash["conflict"]["person"] == "Sam"

    # Overlapping but not identical must also be refused.
    overlap = book_slot("Priya", day, "10:45", duration_minutes=30, path=cal)
    assert overlap["ok"] is False

    # Still exactly one event after the rejected attempts.
    assert len(load(cal)["events"]) == 1


def test_back_to_back_bookings_are_allowed(cal):
    day = _next_weekday(1).isoformat()
    assert book_slot("Sam", day, "11:00", path=cal)["ok"] is True
    adjacent = book_slot("Priya", day, "11:30", path=cal)
    assert adjacent["ok"] is True
    assert len(load(cal)["events"]) == 2


def test_failed_booking_does_not_touch_the_calendar(cal):
    day = _next_weekday(1)
    book_slot("Sam", day.isoformat(), "11:00", path=cal)
    before = cal.read_text(encoding="utf-8")
    book_slot("Priya", day.isoformat(), "11:00", path=cal)  # rejected
    assert cal.read_text(encoding="utf-8") == before


def test_weekend_bookings_are_rejected(cal):
    today = date.today()
    days_to_sat = (5 - today.weekday()) % 7 or 7
    saturday = today + timedelta(days=days_to_sat)
    result = book_slot("Sam", saturday.isoformat(), "11:00", path=cal)
    assert result["ok"] is False
    assert "working days" in result["error"]


def test_out_of_hours_bookings_are_rejected(cal):
    day = _next_weekday(1).isoformat()
    assert book_slot("Sam", day, "08:30", path=cal)["ok"] is False
    assert book_slot("Sam", day, "16:45", duration_minutes=30, path=cal)["ok"] is False
    assert book_slot("Sam", day, "16:30", duration_minutes=30, path=cal)["ok"] is True


def test_check_calendar_excludes_busy_times_from_free_slots(cal):
    day = _next_weekday(1)
    book_slot("Sam", day.isoformat(), "10:00", duration_minutes=60, path=cal)

    slots = free_slots(day, path=cal)
    assert "10:00" not in slots
    assert "10:30" not in slots
    assert "09:00" in slots
    assert "11:00" in slots

    report = check_calendar(day.isoformat(), day.isoformat(), path=cal)
    assert report["ok"] is True
    assert report["days"][0]["busy"][0]["start"] == "10:00"


def test_past_booking_is_rejected(cal):
    yesterday = date.today() - timedelta(days=1)
    result = book_slot("Sam", yesterday.isoformat(), "11:00", path=cal)
    assert result["ok"] is False
    assert "past" in result["error"]
