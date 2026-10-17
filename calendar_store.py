"""Fake JSON calendar store plus the two tools the agent calls.

Tools exposed to the model (see agent.py for the schemas):
  - check_calendar : report busy events and free slots for a date range
  - book_slot      : book a slot, refusing any overlap (never double-book)

The no-double-booking rule is enforced *here*, in code -- not only in the
prompt. `book_slot` re-checks every existing event before writing, so even a
misbehaving model cannot create an overlapping booking.

Bookings are also race-safe: the read-check-write in `book_slot` runs under a
cross-process file lock and writes atomically, so concurrent agents cannot both
claim the same slot.

Times are timezone-aware (IANA names via `zoneinfo`). The calendar has a home
timezone that defines its working hours; events may carry their own timezone;
`book_slot` interprets its `date`/`start` in a caller-supplied timezone. Overlap
detection compares absolute instants, so an event in one zone correctly blocks a
matching instant in another (including across DST changes).

All functions accept an optional `path` so tests can run against a temp file.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import date as _date
from datetime import datetime, timedelta
from datetime import time as _time
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE_DIR = Path(__file__).resolve().parent
CALENDAR_PATH = BASE_DIR / "calendar.json"

WORK_START = _time(9, 0)
WORK_END = _time(17, 0)
SLOT_MINUTES = 30
WEEKDAYS = {0, 1, 2, 3, 4}  # Monday-Friday
DEFAULT_TZ = "UTC"

# Booking lock tuning.
LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT = 15.0  # seconds to wait for the booking lock before giving up
LOCK_POLL = 0.02  # seconds between acquisition attempts
LOCK_STALE = 30.0  # a lock older than this is treated as abandoned

DEFAULT = {
    "owner": "you",
    "timezone": DEFAULT_TZ,
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
    """Write the calendar atomically: temp file in the same dir, then os.replace.

    os.replace is atomic on both Windows and POSIX, so readers never see a
    half-written file and concurrent writers never interleave bytes.
    """
    target = _path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # On Windows another process may briefly hold the target (indexer, AV), so
    # retry the replace a few times before giving up.
    last_error: OSError | None = None
    for _ in range(5):
        try:
            os.replace(tmp, target)
            return
        except PermissionError as exc:  # pragma: no cover - timing dependent
            last_error = exc
            time.sleep(0.02)
    tmp.unlink(missing_ok=True)
    raise last_error if last_error else OSError("failed to write calendar")


@contextmanager
def _file_lock(path=None):
    """Cross-process advisory lock via exclusive creation of ``<path>.lock``.

    Serializes the read-check-write in ``book_slot`` so two callers (threads or
    separate processes) cannot both observe the same free slot and both book it.
    Stale locks left by a crashed process are cleaned up after ``LOCK_STALE``.
    """
    lock_path = Path(f"{_path(path)}{LOCK_SUFFIX}")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + LOCK_TIMEOUT
    handle = None
    while handle is None:
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            # Windows raises PermissionError (not FileExistsError) when the lock
            # is already held; treat both as "someone else has the lock".
            try:
                if time.time() - lock_path.stat().st_mtime > LOCK_STALE:
                    lock_path.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"could not acquire calendar lock: {lock_path}")
            time.sleep(LOCK_POLL)
    try:
        os.write(handle, f"{os.getpid()}\n".encode())
        yield
    finally:
        os.close(handle)
        try:
            lock_path.unlink(missing_ok=True)
        except OSError:
            pass


def _get_tz(name: str) -> ZoneInfo:
    """Resolve an IANA timezone name, raising ValueError if unknown."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise ValueError(f"unknown timezone: {name!r}") from exc


def _home_tz(data: dict) -> ZoneInfo:
    """The calendar's home timezone (falls back to UTC if unset/invalid)."""
    try:
        return _get_tz(data.get("timezone") or DEFAULT_TZ)
    except ValueError:
        return ZoneInfo(DEFAULT_TZ)


def _parse_date(value) -> _date:
    if isinstance(value, _date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _parse_time(value) -> _time:
    if isinstance(value, _time):
        return value
    return datetime.strptime(str(value), "%H:%M").time()


def _overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


def _event_interval(ev: dict, home: ZoneInfo):
    """Return the event's (start, end) as timezone-aware datetimes."""
    day = _parse_date(ev["date"])
    tz = _get_tz(ev["timezone"]) if ev.get("timezone") else home
    start = datetime.combine(day, _parse_time(ev["start"]), tzinfo=tz)
    end = datetime.combine(day, _parse_time(ev["end"]), tzinfo=tz)
    return start, end


def _busy_for_day(data: dict, day: _date, home: ZoneInfo):
    """Events whose start falls on `day` in the home timezone."""
    out = []
    for ev in data["events"]:
        start, end = _event_interval(ev, home)
        if start.astimezone(home).date() == day:
            out.append((start, end, ev))
    out.sort(key=lambda item: item[0])
    return out


def free_slots(day, path=None, slot_minutes: int = SLOT_MINUTES, timezone=None) -> list[str]:
    """Start times ("HH:MM") of free aligned slots on `day`.

    Slots are aligned to the calendar's home timezone; pass `timezone` to render
    them in another zone.
    """
    data = load(path)
    home = _home_tz(data)
    out_tz = _get_tz(timezone) if timezone else home
    day = _parse_date(day)

    busy = [(start, end) for start, end, _ in _busy_for_day(data, day, home)]
    slots: list[str] = []
    cursor = datetime.combine(day, WORK_START, tzinfo=home)
    limit = datetime.combine(day, WORK_END, tzinfo=home)
    step = timedelta(minutes=slot_minutes)
    while cursor + step <= limit:
        nxt = cursor + step
        if not any(_overlap(cursor, nxt, s, e) for s, e in busy):
            slots.append(cursor.astimezone(out_tz).strftime("%H:%M"))
        cursor = nxt
    return slots


def check_calendar(start_date=None, end_date=None, timezone=None, path=None) -> dict:
    """List busy events and free slots for each day in [start_date, end_date].

    Times are reported in the calendar's home timezone by default; pass
    `timezone` to render them in another zone.
    """
    data = load(path)
    home = _home_tz(data)
    try:
        out_tz = _get_tz(timezone) if timezone else home
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    today = datetime.now(home).date()
    start = _parse_date(start_date) if start_date else today
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
                        "start": start_dt.astimezone(out_tz).strftime("%H:%M"),
                        "end": end_dt.astimezone(out_tz).strftime("%H:%M"),
                        "date": start_dt.astimezone(out_tz).date().isoformat(),
                        "title": ev.get("title"),
                    }
                    for start_dt, end_dt, ev in _busy_for_day(data, day, home)
                ],
                "free_slots": free_slots(day, path, timezone=timezone) if working else [],
            }
        )
        day += timedelta(days=1)

    return {
        "ok": True,
        "today": today.isoformat(),
        "timezone": out_tz.key,
        "home_timezone": home.key,
        "working_hours": data.get("working_hours", {"start": "09:00", "end": "17:00"}),
        "slot_minutes": SLOT_MINUTES,
        "days": days,
    }


def book_slot(
    person, date, start, duration_minutes=30, title=None, timezone=None, path=None
) -> dict:
    """Book a slot. Returns {"ok": True, ...} or {"ok": False, "error": ...}.

    `date` and `start` are interpreted in `timezone` (default: the calendar's
    home timezone). Working hours are validated in the home timezone. Any overlap
    with an existing event -- even one in a different timezone -- is rejected.
    The read-check-write runs under a file lock, so concurrent callers cannot
    both book the same slot.
    """
    try:
        input_tz = _get_tz(timezone) if timezone else None
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        day = _parse_date(date)
        start_time = _parse_time(start)
        duration = int(duration_minutes)
    except (ValueError, TypeError) as exc:
        return {"ok": False, "error": f"invalid input: {exc}"}

    if duration <= 0:
        return {"ok": False, "error": "duration_minutes must be positive"}

    with _file_lock(path):
        data = load(path)
        home = _home_tz(data)
        tz = input_tz or home

        start_dt = datetime.combine(day, start_time, tzinfo=tz)
        end_dt = start_dt + timedelta(minutes=duration)
        home_start = start_dt.astimezone(home)
        home_end = end_dt.astimezone(home)

        if home_start.weekday() not in WEEKDAYS:
            return {
                "ok": False,
                "error": (
                    f"{home_start.date().isoformat()} is a {home_start.strftime('%A')} "
                    f"in {home.key}; working days are Mon-Fri"
                ),
            }
        if (
            home_start.time() < WORK_START
            or home_end.time() > WORK_END
            or home_start.date() != home_end.date()
        ):
            return {
                "ok": False,
                "error": (
                    f"slot falls outside working hours 09:00-17:00 ({home.key}); "
                    f"that is {home_start.strftime('%H:%M')}-{home_end.strftime('%H:%M')} there"
                ),
            }
        if start_dt <= datetime.now(tz=tz):
            return {"ok": False, "error": "cannot book a slot in the past"}

        for ev in data["events"]:
            busy_start, busy_end = _event_interval(ev, home)
            if _overlap(start_dt, end_dt, busy_start, busy_end):
                return {
                    "ok": False,
                    "error": "double-booking blocked: slot overlaps an existing event",
                    "conflict": {
                        "person": ev.get("person"),
                        "start": busy_start.astimezone(tz).strftime("%H:%M"),
                        "end": busy_end.astimezone(tz).strftime("%H:%M"),
                        "timezone": tz.key,
                        "title": ev.get("title"),
                    },
                }

        event = {
            "id": f"evt-{uuid.uuid4().hex[:8]}",
            "person": person,
            "date": home_start.date().isoformat(),
            "start": home_start.strftime("%H:%M"),
            "end": home_end.strftime("%H:%M"),
            "timezone": home.key,
            "title": title or f"Meeting with {person}",
        }
        data["events"].append(event)
        data["events"].sort(key=lambda item: (item["date"], item["start"]))
        save(data, path)

    message = (
        f"Booked {duration} min with {person} on {home_start.strftime('%A')} "
        f"{event['date']} {event['start']}-{event['end']} ({home.key})."
    )
    result = {"ok": True, "booked": event, "message": message}
    if tz.key != home.key:
        result["requested_time"] = (
            f"{start_dt.strftime('%Y-%m-%d %H:%M')}-{end_dt.strftime('%H:%M')} {tz.key}"
        )
    return result
