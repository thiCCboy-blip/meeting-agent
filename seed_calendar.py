"""Generate a sample calendar.json seeded on next week's weekdays.

Dates are computed relative to today, so the demo never goes stale. The
calendar's home timezone defaults to UTC; override it with CALENDAR_TZ, e.g.
``CALENDAR_TZ=Asia/Kolkata python seed_calendar.py``.

Usage: python seed_calendar.py [--path calendar.json]
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

# (weekday offset from next Monday, person, start, end, title)
SEED = [
    (0, "Team", "09:00", "10:00", "Team sync"),
    (1, "Priya", "10:00", "11:00", "1:1 Priya"),
    (2, "Self", "14:00", "16:00", "Focus block"),
    (3, "Team", "09:30", "10:00", "Standup"),
    (4, "Team", "15:00", "16:00", "Retro"),
]

DEFAULT_TZ = "UTC"


def next_monday(today: date | None = None) -> date:
    today = today or date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def sample_calendar(today: date | None = None, timezone: str = DEFAULT_TZ) -> dict:
    monday = next_monday(today)
    events = []
    for index, (offset, person, start, end, title) in enumerate(SEED, 1):
        day = monday + timedelta(days=offset)
        events.append(
            {
                "id": f"evt-seed{index:02d}",
                "person": person,
                "date": day.isoformat(),
                "start": start,
                "end": end,
                "title": title,
            }
        )
    return {
        "owner": "you",
        "timezone": timezone,
        "working_hours": {"start": "09:00", "end": "17:00"},
        "slot_minutes": 30,
        "events": events,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the sample calendar.")
    parser.add_argument("--path", default=str(BASE_DIR / "calendar.json"))
    parser.add_argument("--timezone", default=os.environ.get("CALENDAR_TZ", DEFAULT_TZ))
    args = parser.parse_args(argv)

    data = sample_calendar(timezone=args.timezone)
    Path(args.path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"Wrote {len(data['events'])} events to {args.path} (timezone={data['timezone']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
