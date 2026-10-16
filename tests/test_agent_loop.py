"""Tests for the agent loop itself.

The chat API is mocked, so these run offline and assert *agent behaviour*:
it must inspect the calendar before booking, and it cannot double-book even
when told to.
"""

import json
from datetime import timedelta

import pytest

import calendar_store
from agent import run_agent
from seed_calendar import next_monday


def _tool_response(*calls):
    tool_calls = [
        {
            "id": f"call_{index}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }
        for index, (name, args) in enumerate(calls, 1)
    ]
    return {
        "choices": [{"message": {"role": "assistant", "content": None, "tool_calls": tool_calls}}]
    }


def _text_response(text):
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture
def scripted(monkeypatch, tmp_path):
    """Point the calendar at a temp file and feed the agent canned responses."""
    monkeypatch.setattr(calendar_store, "CALENDAR_PATH", tmp_path / "cal.json")

    def install(responses):
        queue = list(responses)

        def fake_chat(messages, model, api_key, base_url, effort):
            return queue.pop(0)

        monkeypatch.setattr("agent._chat", fake_chat)

    return install


def _tool_names(result):
    names = []
    for message in result["messages"]:
        for call in message.get("tool_calls") or []:
            names.append(call["function"]["name"])
    return names


def _tool_results(result):
    return [json.loads(m["content"]) for m in result["messages"] if m["role"] == "tool"]


def _run():
    return run_agent(
        "Book 30 minutes with Sam next week, not Monday",
        model="m",
        api_key="k",
        base_url="http://example",
        verbose=False,
    )


def test_agent_checks_calendar_before_booking(scripted):
    tuesday = (next_monday() + timedelta(days=1)).isoformat()
    scripted(
        [
            _tool_response(("check_calendar", {"start_date": tuesday, "end_date": tuesday})),
            _tool_response(
                (
                    "book_slot",
                    {"person": "Sam", "date": tuesday, "start": "11:00", "duration_minutes": 30},
                )
            ),
            _text_response("Booked 30 min with Sam on Tuesday at 11:00."),
        ]
    )

    result = _run()

    assert "Booked" in result["reply"]
    assert _tool_names(result) == ["check_calendar", "book_slot"]
    events = calendar_store.load()["events"]
    assert len(events) == 1
    assert events[0]["person"] == "Sam"


def test_agent_cannot_double_book_even_when_it_tries(scripted):
    tuesday = (next_monday() + timedelta(days=1)).isoformat()
    scripted(
        [
            _tool_response(("book_slot", {"person": "Sam", "date": tuesday, "start": "11:00"})),
            _tool_response(("book_slot", {"person": "Priya", "date": tuesday, "start": "11:00"})),
            _text_response("Second booking was rejected."),
        ]
    )

    result = _run()

    results = _tool_results(result)
    assert results[0]["ok"] is True
    assert results[1]["ok"] is False
    assert "double-booking" in results[1]["error"]
    assert len(calendar_store.load()["events"]) == 1


def test_agent_stops_at_max_steps(scripted):
    tuesday = (next_monday() + timedelta(days=1)).isoformat()
    # Always ask to book the same thing; the loop must terminate.
    scripted(
        [
            _tool_response(("check_calendar", {"start_date": tuesday, "end_date": tuesday}))
            for _ in range(20)
        ]
    )

    result = run_agent(
        "loop forever",
        model="m",
        api_key="k",
        base_url="http://example",
        max_steps=3,
        verbose=False,
    )

    assert result["steps"] == 3
    assert "error" in result
