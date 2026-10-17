# Meeting-booking agent

[![CI](https://github.com/thiCCboy-blip/meeting-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/thiCCboy-blip/meeting-agent/actions/workflows/ci.yml)

A tool-using agent that books meetings from plain-English requests against a
fake calendar stored in `calendar.json`. It exposes two tools to the model and
enforces a hard **never-double-book** rule in code.

- `check_calendar(start_date, end_date)` — busy events and free 30-min slots per day
- `book_slot(person, date, start, duration_minutes=30, title=None)` — books a slot,
  **rejecting any overlap** with an existing event

The agent and tools use only the Python standard library. It runs on any
OpenAI-compatible chat API and defaults to a **free** provider (Groq).

## What makes this different

Most "booking agent" demos check-then-write, so two concurrent bookings silently
double-book. Here the guarantee is **race-safe**, not just prompt-level:

- **Overlap prevention in code** — `book_slot` re-reads every event and refuses
  overlaps; the model cannot talk its way past it.
- **Race-safe by construction** — the read-check-write runs under a cross-process
  file lock with atomic writes (`os.replace`), so parallel agents cannot both claim
  a slot. Proven by concurrency tests that fire 24 threads and 8 OS processes at the
  *same* slot and assert exactly one winner.
- **Agent behaviour is tested** — with a mocked chat API, the loop is shown to
  inspect the calendar before booking and to fail safe when it tries to double-book.

> **Resume line:** Built a race-safe, tool-using meeting agent that books from
> plain-English requests — zero double-bookings, holding under concurrent bookings ·
> github.com/thiCCboy-blip/meeting-agent

## Quick start

```bash
cd meeting-agent
cp .env.example .env     # put your key in .env (Groq is free: console.groq.com/keys)
python seed_calendar.py  # write a sample calendar relative to today (optional)
python agent.py "Book 30 minutes with Sam next week, not Monday"
```

Example run:

```
Request: Book 30 minutes with Sam next week, not Monday

  [tool] check_calendar({"start_date": "2026-10-20", "end_date": "2026-10-23"})
  [result] {"ok": true, "today": "2026-10-17", ...}
  [tool] book_slot({"person": "Sam", "date": "2026-10-20", "start": "09:00", "duration_minutes": 30})
  [result] {"ok": true, "message": "Booked 30 min with Sam on Tuesday 2026-10-20 09:00-09:30."}

Booked 30 minutes with Sam on Tuesday 2026-10-20 at 09:00.
```

Other commands:

```bash
python agent.py                     # interactive prompt
python agent.py --show-calendar     # print the next two weeks
python seed_calendar.py             # (re)generate the sample calendar.json
python -m pytest                    # run the test suite
python -m ruff check .              # lint  (pip install ruff)
python -m ruff format --check .     # formatting
```

## Configuration (`.env` or environment variables)

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | required — the key for whichever provider you use |
| `OPENAI_MODEL` | `llama-3.3-70b-versatile` | any tool-calling model |
| `OPENAI_BASE_URL` | `https://api.groq.com/openai/v1` | point at another provider |
| `OPENAI_REASONING_EFFORT` | _(unset)_ | set `none` for OpenAI reasoning models (e.g. `gpt-6-luna`) that reject tool calls otherwise |

**Free providers** — just set `OPENAI_BASE_URL` + `OPENAI_MODEL` (see `.env.example`):
Groq (`llama-3.3-70b-versatile`), OpenRouter free models, or Google Gemini.
OpenAI works too, but is not free.

### Secrets

`.env` is git-ignored (root and project `.gitignore`) — never commit a real key.
Copy `.env.example` and keep only `OPENAI_API_KEY` locally. If a key is ever
committed or shared, rotate it.

## How it works

1. `agent.py` sends the request plus JSON tool schemas to the chat API.
2. The model calls `check_calendar`, receives busy/free data, and chooses a slot.
3. It calls `book_slot`, which takes a cross-process lock, **re-reads the calendar,
   and refuses overlaps**, then writes atomically — the no-double-book rule lives in
   `calendar_store.book_slot`, not in the prompt, so neither a misbehaving model nor
   a concurrent agent can create a conflict.
4. The loop repeats until the model replies without a tool call, then prints a
   confirmation. It stops after `MAX_STEPS` to avoid runaway loops.

Working days are Mon-Fri, working hours 09:00-17:00, slots are 30 minutes.
`seed_calendar.py` writes the sample events relative to *today*, so the demo
never goes stale.

## Tests

```
$ python -m pytest
14 passed
```

- `tests/test_calendar_store.py` — the double-book rule (identical and partial
  overlaps rejected, back-to-back allowed, calendar untouched on failure),
  working hours, weekends, past dates, free-slot computation.
- `tests/test_agent_loop.py` — drives `run_agent` with a **mocked chat API**:
  the agent inspects the calendar before booking, **cannot double-book even when
  told to**, and the loop terminates at `MAX_STEPS`.
- `tests/test_concurrency.py` — the race-safety guarantee: 24 threads and 8
  separate OS processes all race for one slot; exactly one wins, and no lock or
  temp files are left behind.

## Continuous integration

`.github/workflows/ci.yml` runs `ruff check`, `ruff format --check`, and
`pytest` on every push and pull request, across Python 3.10-3.13.

> The workflow lives inside this project so it moves with it if `meeting-agent`
> is published as its own repository. If it stays a subfolder of a larger repo,
> move `.github/workflows/ci.yml` to that repo's root and add
> `working-directory: meeting-agent` to each step.
