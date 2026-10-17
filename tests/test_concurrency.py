"""Concurrency tests: the race-safe booking guarantee.

Many callers -- threads in one process, then separate OS processes -- all try
to book the *same* slot at once. Exactly one must win and the rest must be
rejected as overlaps. On a plain check-then-write this fails; it only passes
because `book_slot` serializes on a file lock and writes atomically.
"""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

from calendar_store import book_slot, load
from seed_calendar import next_monday

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _contested_slot():
    tuesday = (next_monday() + timedelta(days=1)).isoformat()
    return tuesday, "11:00"


def test_threads_cannot_double_book(tmp_path):
    cal = tmp_path / "calendar.json"
    date, start = _contested_slot()

    def attempt(_):
        return book_slot("Sam", date, start, path=cal)

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(24)))

    winners = [r for r in results if r["ok"]]
    losers = [r for r in results if not r["ok"]]
    assert len(winners) == 1
    assert len(losers) == 23
    assert all("double-booking" in r["error"] for r in losers)
    assert len(load(cal)["events"]) == 1


def test_processes_cannot_double_book(tmp_path):
    cal = tmp_path / "calendar.json"
    date, start = _contested_slot()

    script = (
        "import json, sys;"
        "from calendar_store import book_slot;"
        "print(json.dumps(book_slot('Sam', sys.argv[1], sys.argv[2], path=sys.argv[3])))"
    )
    env = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT))
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script, date, start, str(cal)],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(8)
    ]

    outputs = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, err.decode()
        outputs.append(json.loads(out.decode().strip().splitlines()[-1]))

    assert sum(1 for o in outputs if o["ok"]) == 1
    assert len(load(cal)["events"]) == 1
    # No stray lock or temp files left behind.
    assert [p.name for p in tmp_path.iterdir()] == ["calendar.json"]


def test_atomic_save_leaves_no_temp_files(tmp_path):
    cal = tmp_path / "calendar.json"
    date, start = _contested_slot()

    assert book_slot("Sam", date, start, path=cal)["ok"] is True
    assert [p.name for p in tmp_path.iterdir()] == ["calendar.json"]
