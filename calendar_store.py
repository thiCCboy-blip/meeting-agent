"""Fake JSON calendar store plus the two tools the agent calls.

Tools exposed to the model (see agent.py for the schemas):
  - check_calendar : report busy events and free slots for a date range
  - book_slot      : book a slot, refusing any overlap (never double-book)

The no-double-booking rule is enforced *here*, in code -- not only in the
prompt. `book_slot` re-checks every existing event before writing, so even a
misbehaving model cannot create an overlapping booking.

All functions accept an optional `path` so tests can run against a temp file.
"""

from __future__ import annotations

import json
import uuid
from datetime import date as _date
from datetime import datetime, timedelta
from datetime import time as _time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CALENDAR_PATH = BASE_DIR / "calendar.json"

WORK_START = _time(9, 0)
WORK_END = _time(17, 0)
SLOT_MINUTES = 30
WEEKDAYS = {0, 1, 2, 3, 4}  # Monday-Friday

DEFAULT = {
    "owner": "you",
    "timezone": "local",
    "working_hours": {"start": "09:00", "end": "17:00"},
    "slot_minutes": SLOT_MINUTES,
    "events": [],
}


def _path(path=None) -> Path:
    return Path(path) if path else CALENDAR_PATH


def load(path=None) -> dict:
    p = _path(path)
    if not p.exists():
        data = json.loads(json.dumps(DEFAULT))
        save(data, path)
        return data
    return json.loads(p.read_text(encoding="utf-8"))


def save(data: dict, path=None) -> None:
    _path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")


def _parse_date(value) -> _date:
    if isinstance(value, _date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _parse_time(value) -> _time:
    if isinstance(value, _time):
        return value
    return datetime.strptime(str(value), "%H:%M").time()


def _dt(day: _date, t: _time) -> datetime:
    return datetime.combine(day, t)


def _overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _interval(ev: dict):
    day = _parse_date(ev["date"])
    return day, _dt(day, _parse_time(ev["start"])), _dt(day, _parse_time(ev["end"]))


def _busy_for_day(data: dict, day: _date):
    out = []
    for ev in data["events"]:
        ev_day, start, end = _interval(ev)
        if ev_day == day:
            out.append((start, end, ev))
    out.sort(key=lambda item: item[0])
    return out


def free_slots(day, path=None, slot_minutes: int = SLOT_MINUTES) -> list[str]:
    """Return the start times ("HH:MM") of free aligned slots on `day`."""
    data = load(path)
    busy = [(start, end) for start, end, _ in _busy_for_day(data, _parse_date(day))]
    slots: list[str] = []
    cursor = _dt(_parse_date(day), WORK_START)
    day_end = _dt(_parse_date(day), WORK_END)
    while cursor + timedelta(minutes=slot_minutes) <= day_end:
        nxt = cursor + timedelta(minutes=slot_minutes)
        if not any(_overlap(cursor, nxt, s, e) for s, e in busy):
            slots.append(cursor.strftime("%H:%M"))
        cursor = nxt
    return slots


def check_calendar(start_date=None, end_date=None, path=None) -> dict:
    """List busy events and free slots for each day in [start_date, end_date]."""
    data = load(path)
    start = _parse_date(start_date) if start_date else _date.today()
    end = _parse_date(end_date) if end_date else start
    if end < start:
        return {"ok": False, "error": "end_date is before start_date"}
    if (end - start).days > 31:
        return {"ok": False, "error": "range too large (max 31 days)"}

    days = []
    day = start
    while day <= end:
        working = day.weekday() in WEEKDAYS
        days.append(
            {
                "date": day.isoformat(),
                "weekday": day.strftime("%A"),
                "working_day": working,
                "busy": [
                    {
                        "person": ev.get("person"),
                        "start": start_dt.strftime("%H:%M"),
                        "end": end_dt.strftime("%H:%M"),
                        "title": ev.get("title"),
                    }
                    for start_dt, end_dt, ev in _busy_for_day(data, day)
                ],
                "free_slots": free_slots(day, path) if working else [],
            }
        )
        day += timedelta(days=1)

    return {
        "ok": True,
        "today": _date.today().isoformat(),
        "working_hours": data.get("working_hours", {"start": "09:00", "end": "17:00"}),
        "slot_minutes": SLOT_MINUTES,
        "days": days,
    }


def book_slot(person, date, start, duration_minutes=30, title=None, path=None) -> dict:
    """Book a slot. Returns {"ok": True, ...} or {"ok": False, "error": ...}.

    Refuses to double-book: any overlap with an existing event is rejected.
    """
    data = load(path)
    try:
        day = _parse_date(date)
        start_time = _parse_time(start)
        duration = int(duration_minutes)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": f"invalid input: {exc}"}

    if duration <= 0:
        return {"ok": False, "error": "duration_minutes must be positive"}
    if day.weekday() not in WEEKDAYS:
        return {
            "ok": False,
            "error": f"{day.isoformat()} is a {day.strftime('%A')}; working days are Mon-Fri",
        }
    if day < _date.today():
        return {"ok": False, "error": "cannot book a slot in the past"}

    start_dt = _dt(day, start_time)
    end_dt = start_dt + timedelta(minutes=duration)
    if start_time < WORK_START or end_dt > _dt(day, WORK_END) or end_dt.date() != day:
        return {"ok": False, "error": "slot falls outside working hours 09:00-17:00"}

    for busy_start, busy_end, ev in _busy_for_day(data, day):
        if _overlap(start_dt, end_dt, busy_start, busy_end):
            return {
                "ok": False,
                "error": "double-booking blocked: slot overlaps an existing event",
                "conflict": {
                    "person": ev.get("person"),
                    "start": busy_start.strftime("%H:%M"),
                    "end": busy_end.strftime("%H:%M"),
                    "title": ev.get("title"),
                },
            }

    event = {
        "id": f"evt-{uuid.uuid4().hex[:8]}",
        "person": person,
        "date": day.isoformat(),
        "start": start_dt.strftime("%H:%M"),
        "end": end_dt.strftime("%H:%M"),
        "title": title or f"Meeting with {person}",
    }
    data["events"].append(event)
    data["events"].sort(key=lambda item: (item["date"], item["start"]))
    save(data, path)
    return {
        "ok": True,
        "booked": event,
        "message": (
            f"Booked {duration} min with {person} on {day.strftime('%A')} "
            f"{day.isoformat()} {event['start']}-{event['end']}."
        ),
    }
