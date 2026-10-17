"""MCP server exposing the calendar tools over stdio JSON-RPC.

Implements the Model Context Protocol wire protocol by hand (JSON-RPC 2.0
over stdin/stdout, newline-delimited) using only the standard library, so
there is no dependency to install. Any MCP client (opencode, Claude
Desktop, etc.) can launch this process, list the two tools, and call them:

  - check_calendar : busy events + free 30-min slots for a date range
  - book_slot      : book a slot, refusing any overlap (never double-book)

Run:
  python mcp_server.py

Clients launch it as a stdio server, e.g. in opencode.json:
  { "type": "local", "command": ["python", "mcp_server.py"], "enabled": true }
"""

from __future__ import annotations

import json
import sys

from calendar_store import book_slot, check_calendar

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "calendar-mcp", "version": "1.0.0"}

TOOLS = [
    {
        "name": "check_calendar",
        "description": (
            "Look up the calendar for a date range. Returns each day's busy "
            "events and its list of free 30-minute slot start times (HH:MM). "
            "Always call this before booking. Times are shown in `timezone` "
            "(default: the calendar's home timezone)."
        ),
        "inputSchema": {
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
    {
        "name": "book_slot",
        "description": (
            "Book a meeting. The slot must be on a working day (Mon-Fri, "
            "09:00-17:00) and must not overlap any existing event -- "
            "overlapping bookings are rejected. `date`/`start` are interpreted "
            "in `timezone` (default: the calendar's home timezone)."
        ),
        "inputSchema": {
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
]

DISPATCH = {"check_calendar": check_calendar, "book_slot": book_slot}


def handle(method: str, params: dict):
    """Dispatch a JSON-RPC method; returns (result, error)."""
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": SERVER_INFO,
        }, None

    if method == "notifications/initialized":
        return None, None  # notification: no response

    if method == "tools/list":
        return {"tools": TOOLS}, None

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = DISPATCH.get(name)
        if fn is None:
            return None, {"code": -32602, "message": f"unknown tool: {name}"}
        try:
            result = fn(**args)
        except TypeError as exc:
            result = {"ok": False, "error": f"bad arguments: {exc}"}
        text = json.dumps(result)
        return {"content": [{"type": "text", "text": text}], "isError": False}, None

    if method == "ping":
        return {}, None

    return None, {"code": -32601, "message": f"method not found: {method}"}


def main() -> int:
    """Read newline-delimited JSON-RPC from stdin, write responses to stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        result, error = handle(method, params)

        if msg_id is None:
            continue  # notification (e.g. notifications/initialized): no reply
        if error is not None:
            reply = {"jsonrpc": "2.0", "id": msg_id, "error": error}
        else:
            reply = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
