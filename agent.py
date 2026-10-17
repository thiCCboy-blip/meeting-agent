"""Meeting-booking agent: a tool-use loop over an OpenAI-compatible chat API.

The agent reads a plain-English request, calls tools to inspect/update a fake
JSON calendar, and confirms the booking. It exposes exactly two tools:

  - check_calendar : look up busy events and free slots for a date range
  - book_slot      : book a slot, refusing any overlap (never double-book)

Runs against any OpenAI-compatible endpoint. Defaults to Groq's free tier;
point OPENAI_BASE_URL at OpenAI/OpenRouter/Gemini to use those instead.

Configuration (environment variables, or a local .env file):
  OPENAI_API_KEY            required (the key for whichever provider you use)
  OPENAI_MODEL              default "llama-3.3-70b-versatile" (Groq, free)
  OPENAI_BASE_URL           default "https://api.groq.com/openai/v1"
  OPENAI_REASONING_EFFORT   optional; unset by default. Set to "none" for OpenAI
                            reasoning models (e.g. gpt-6-luna) that reject tool
                            calls otherwise.

Usage:
  python agent.py "Book 30 minutes with Sam next week, not Monday"
  python agent.py                     # interactive prompt
  python agent.py --show-calendar     # print the next 2 weeks of the calendar
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

from calendar_store import _home_tz, book_slot, check_calendar, load

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_MODEL = "llama-3.3-70b-versatile"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
MAX_STEPS = 8

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_calendar",
            "description": (
                "Look up the calendar for a date range. Returns each day's busy "
                "events and its list of free 30-minute slot start times (HH:MM). "
                "Always call this before booking. Times are shown in `timezone` "
                "(default: the calendar's home timezone)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_date": {"type": "string", "description": "First day, YYYY-MM-DD"},
                    "end_date": {
                        "type": "string",
                        "description": "Last day, YYYY-MM-DD (inclusive)",
                    },
                    "timezone": {
                        "type": "string",
                        "description": (
                            "IANA timezone to display times in, e.g. 'Asia/Kolkata'. "
                            "Defaults to the calendar's home timezone."
                        ),
                    },
                },
                "required": ["start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_slot",
            "description": (
                "Book a meeting. The slot must be on a working day (Mon-Fri, "
                "09:00-17:00) and must not overlap any existing event -- "
                "overlapping bookings are rejected. `date`/`start` are interpreted "
                "in `timezone` (default: the calendar's home timezone)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "person": {"type": "string", "description": "Who the meeting is with"},
                    "date": {"type": "string", "description": "Day to book, YYYY-MM-DD"},
                    "start": {"type": "string", "description": "Start time, HH:MM (24h)"},
                    "duration_minutes": {
                        "type": "integer",
                        "description": "Meeting length in minutes (default 30)",
                    },
                    "title": {"type": "string", "description": "Optional meeting title"},
                    "timezone": {
                        "type": "string",
                        "description": (
                            "IANA timezone that `date`/`start` are expressed in, e.g. "
                            "'America/New_York'. Defaults to the calendar's home timezone."
                        ),
                    },
                },
                "required": ["person", "date", "start"],
            },
        },
    },
]

DISPATCH = {"check_calendar": check_calendar, "book_slot": book_slot}


def system_prompt() -> str:
    today = date.today()
    home_tz = _home_tz(load()).key
    return (
        "You are a scheduling assistant that books meetings by calling tools.\n"
        f"Today is {today.isoformat()} ({today.strftime('%A')}).\n"
        f"The calendar's home timezone is {home_tz}; all times are in that zone "
        "unless the user names a different one.\n\n"
        "Rules:\n"
        "1. ALWAYS call check_calendar before book_slot so you book a genuinely free slot.\n"
        "2. NEVER double-book. Only ask for a slot that check_calendar reported as free. "
        "If book_slot rejects an overlap, pick a different free slot and try again.\n"
        "3. Respect every constraint in the request: duration, attendee, and exclusions "
        'such as "not Monday" or a preferred time of day.\n'
        "4. Working days are Mon-Fri, working hours 09:00-17:00, slots are 30 minutes.\n"
        '5. Resolve relative dates (e.g. "next week") into concrete dates from today.\n'
        '6. If the user mentions a timezone (e.g. "my 6pm" in IST), pass that IANA '
        "name as the `timezone` argument to the tools so times are interpreted and "
        "reported in their zone.\n"
        "7. Finish with a short confirmation: who, weekday, date, and time. If the request "
        "cannot be met, say why and propose the closest alternative.\n"
    )


def load_env(path: Path | None = None) -> None:
    """Load KEY=VALUE lines from .env without overriding real env vars."""
    env_path = path or (BASE_DIR / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _chat(messages: list, model: str, api_key: str, base_url: str, effort: str) -> dict:
    payload = {"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto"}
    if effort:
        payload["reasoning_effort"] = effort
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"API error {exc.code}: {detail}") from exc


def run_agent(
    request: str,
    *,
    model: str,
    api_key: str,
    base_url: str,
    effort: str = "",
    max_steps: int = MAX_STEPS,
    verbose: bool = True,
) -> dict:
    """Run the tool-use loop until the model answers without a tool call."""
    messages = [
        {"role": "system", "content": system_prompt()},
        {"role": "user", "content": request},
    ]

    for step in range(1, max_steps + 1):
        response = _chat(messages, model, api_key, base_url, effort)
        message = response["choices"][0]["message"]
        tool_calls = message.get("tool_calls")

        assistant = {"role": "assistant", "content": message.get("content") or ""}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        messages.append(assistant)

        if not tool_calls:
            return {"reply": message.get("content") or "", "steps": step, "messages": messages}

        for call in tool_calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            if verbose:
                print(f"  [tool] {name}({json.dumps(args)})")
            if name in DISPATCH:
                try:
                    result = DISPATCH[name](**args)
                except TypeError as exc:
                    result = {"ok": False, "error": f"bad arguments: {exc}"}
            else:
                result = {"ok": False, "error": f"unknown tool: {name}"}
            if verbose:
                print(f"  [result] {json.dumps(result)}")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": json.dumps(result),
                }
            )

    return {"reply": "", "steps": max_steps, "error": "max steps reached", "messages": messages}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Book meetings from plain-English requests.")
    parser.add_argument(
        "request",
        nargs="*",
        help='e.g. "Book 30 minutes with Sam next week, not Monday"',
    )
    parser.add_argument("--model", default=None)
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--show-calendar", action="store_true", help="print the next 2 weeks")
    args = parser.parse_args(argv)

    if args.show_calendar:
        today = date.today()
        print(
            json.dumps(
                check_calendar(today.isoformat(), (today + timedelta(days=14)).isoformat()),
                indent=2,
            )
        )
        return 0

    load_env()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("error: set OPENAI_API_KEY (env var or .env file)", file=sys.stderr)
        return 2
    model = args.model or os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)
    base_url = args.base_url or os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL)
    effort = os.environ.get("OPENAI_REASONING_EFFORT", "")

    request = " ".join(args.request).strip()
    if not request:
        request = input("Request: ").strip()
    if not request:
        print("error: empty request", file=sys.stderr)
        return 2

    print(f"Request: {request}\n")
    result = run_agent(request, model=model, api_key=api_key, base_url=base_url, effort=effort)
    print(f"\n{result['reply']}")
    if "error" in result:
        print(f"(stopped: {result['error']})", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
