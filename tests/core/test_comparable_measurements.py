"""Comparable cycles use tool-result boundaries, never inferred decoding speed."""

import json

import pytest

from core.event_model import EventStatus, ModelCallEvent, TurnEvent
from core.measurements import response_measurements, trigger_measurements
from tracing.claude_code.hooks.span_renderer import render_event_graph
from tracing.claude_code.hooks.transcript import parse_claude_transcript
from tracing.codex.hooks.transcript import _extract_turn_from_rollout
from tracing.codex.hooks.graph import render_turn


def stamp(second):
    return f"2026-01-01T00:00:{second:02d}Z"


def write_rows(tmp_path, rows):
    path = tmp_path / "synthetic.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows))
    return path


@pytest.mark.parametrize(
    "value,known", [(0, True), (4200, True), (None, False), (True, False), (-1, False), (1.5, False), ("12", False)]
)
def test_native_ttft_is_matching_turn_only_and_keeps_zero(tmp_path, value, known):
    rows = [
        {"type": "event_msg", "timestamp": stamp(1), "payload": {"type": "task_started", "turn_id": "t"}},
        {
            "type": "event_msg",
            "timestamp": stamp(2),
            "payload": {"type": "task_complete", "turn_id": "other", "time_to_first_token_ms": 99},
        },
        {
            "type": "event_msg",
            "timestamp": stamp(5),
            "payload": {"type": "task_complete", "turn_id": "t", "time_to_first_token_ms": value},
        },
        {
            "type": "event_msg",
            "timestamp": stamp(6),
            "payload": {"type": "task_complete", "turn_id": "t", "time_to_first_token_ms": 999},
        },
    ]
    turn = _extract_turn_from_rollout(write_rows(tmp_path, rows), "t")
    measurements = turn.get("measurements", {})
    assert ("turn.time_to_first_token_ms" in measurements) is known
    if known:
        assert measurements["turn.time_to_first_token_ms"] == value
        payload = render_turn("session", "t", turn, lambda *_: ("openai", "openai"))
        spans = [s for rs in payload["resourceSpans"] for scope in rs["scopeSpans"] for s in scope["spans"]]
        measured = [s for s in spans if any(a["key"] == "turn.time_to_first_token_ms" for a in s["attributes"])]
        assert len(measured) == 1
        assert measured[0].get("parentSpanId", "") == ""


@pytest.mark.parametrize("completion", [None, {"type": "task_complete"}, {"type": "task_complete", "turn_id": "other"}])
def test_missing_or_unmatched_completion_has_no_ttft(tmp_path, completion):
    rows = [{"type": "event_msg", "timestamp": stamp(1), "payload": {"type": "task_started", "turn_id": "t"}}]
    if completion is not None:
        rows.append(
            {"type": "event_msg", "timestamp": stamp(5), "payload": dict(completion, time_to_first_token_ms=20)}
        )
    turn = _extract_turn_from_rollout(write_rows(tmp_path, rows), "t")
    assert "turn.time_to_first_token_ms" not in turn.get("measurements", {})


@pytest.mark.parametrize("interrupt,omit_usage", [(False, False), (True, False), (False, True)])
def test_claude_cycle_coalesces_final_usage_without_inventing_reasoning(tmp_path, interrupt, omit_usage):
    rows = [
        {
            "type": "assistant",
            "timestamp": stamp(2),
            "message": {
                "id": "a",
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "tool", "name": "Read", "input": {}}],
            },
        },
        {
            "type": "user",
            "timestamp": stamp(10),
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tool", "content": "ok"}]},
        },
        {
            "type": "assistant",
            "timestamp": stamp(12),
            "effort": "medium",
            "message": {
                "id": "b",
                "model": "claude-opus-5-5",
                "role": "assistant",
                "usage": {"output_tokens": 50, "speed": "standard", "output_tokens_details": {"thinking_tokens": 0}},
                "content": [],
            },
        },
        {
            "type": "assistant",
            "timestamp": stamp(15),
            "effort": "medium",
            "message": {
                "id": "b",
                "model": "claude-opus-5-5",
                "role": "assistant",
                "usage": {"output_tokens": 100, "output_tokens_details": {"thinking_tokens": 40}, "speed": "standard"},
                "content": [],
            },
        },
    ]
    if interrupt:
        rows.insert(-1, {"type": "user", "timestamp": stamp(13), "message": {"role": "user", "content": "new request"}})
    if omit_usage:
        del rows[-1]["message"]["usage"]
        del rows[-1]["effort"]
    root = TurnEvent("root", "session", "turn", 0, 0, 20000, EventStatus.COMPLETED, input="Goal check-in: continue")
    graph = parse_claude_transcript(write_rows(tmp_path, rows), root)
    models = [event for event in graph.events if isinstance(event, ModelCallEvent)]
    attrs = models[-1].measurements
    assert ("llm.response_cycle.duration_ms" in attrs) is not interrupt
    if not interrupt:
        assert attrs["llm.response_cycle.duration_ms"] == 5000
    assert attrs["llm.reasoning_effort"] == "medium"
    assert attrs["llm.service_tier"] == "standard"
    assert attrs["llm.token_count.completion_details.reasoning"] == (0 if omit_usage else 40)
    assert attrs["llm.token_count.completion_details.non_reasoning"] == (50 if omit_usage else 60)
    assert "llm.response_cycle.duration_ms" not in models[0].measurements
    payload = render_event_graph(graph, trace_id="a" * 32)
    spans = [span for rs in payload["resourceSpans"] for scope in rs["scopeSpans"] for span in scope["spans"]]
    assert (
        any(any(a["key"] == "llm.response_cycle.duration_ms" for a in span["attributes"]) for span in spans)
        is not interrupt
    )


@pytest.mark.parametrize("reasoning", [None, -1, True, 101, "0"])
def test_unknown_or_inconsistent_reasoning_is_not_reported_as_zero(reasoning):
    attrs = response_measurements(output=100, reasoning=reasoning)
    assert "llm.token_count.completion_details.reasoning" not in attrs
    assert "llm.token_count.completion_details.non_reasoning" not in attrs


def test_recorded_zero_and_invalid_settings_are_preserved_honestly():
    attrs = response_measurements(effort=[], tier={}, output=100, reasoning=0)
    assert attrs == {
        "llm.token_count.completion_details.reasoning": 0,
        "llm.token_count.completion_details.non_reasoning": 100,
    }
    assert "llm.response_cycle.duration_ms" not in response_measurements(output=None, start=1, end=2)


@pytest.mark.parametrize(
    "prompt,expected",
    [
        ("Goal check-in: continue", "goal_check"),
        ("You have 1 orchestration message. Run check", "orchestration_notification"),
        ("<task-notification>done", "task_notification"),
        ("", "unknown"),
        ("implement ticket", "human"),
    ],
)
def test_turn_markers_are_estimates_not_authority(prompt, expected):
    attrs = trigger_measurements(prompt)
    assert attrs["turn.trigger"] == expected
    assert attrs["turn.trigger_source"] in {"transcript_marker_estimate", "unavailable"}


@pytest.mark.parametrize("interrupt,late_result", [(False, False), (True, False), (False, True)])
def test_codex_cycle_includes_thinking_and_resets_on_new_prompt(tmp_path, interrupt, late_result):
    def row(second, kind, payload):
        return {"timestamp": stamp(second), "type": kind, "payload": payload}

    rows = [
        row(0, "session_meta", {"id": "session"}),
        row(
            0,
            "event_msg",
            {
                "type": "thread_settings_applied",
                "thread_settings": {"service_tier": "priority", "reasoning_effort": "high"},
            },
        ),
        row(1, "event_msg", {"type": "task_started", "turn_id": "turn"}),
        row(1, "turn_context", {"turn_id": "turn", "model": "gpt-6.1-sol", "effort": "high"}),
        row(2, "response_item", {"type": "function_call", "name": "read", "call_id": "tool", "arguments": "{}"}),
        row(
            3,
            "token_usage_record",
            {"turn_id": "turn", "response_id": "a", "usage": {"input_tokens": 20, "output_tokens": 10}},
        ),
        row(10, "response_item", {"type": "function_call_output", "call_id": "tool", "output": "ok"}),
    ]
    if interrupt:
        rows.append(
            row(
                11,
                "response_item",
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "new request"}]},
            )
        )
    rows += [
        row(13, "response_item", {"type": "reasoning", "summary": []}),
        row(
            14,
            "response_item",
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
        ),
        row(
            15,
            "token_usage_record",
            {
                "turn_id": "turn",
                "response_id": "b",
                "usage": {"input_tokens": 20, "output_tokens": 100, "reasoning_output_tokens": 40},
            },
        ),
        row(
            16,
            "token_usage_record",
            {
                "turn_id": "turn",
                "response_id": "b",
                "usage": {"input_tokens": 20, "output_tokens": 100, "reasoning_output_tokens": 40},
            },
        ),
        row(17, "event_msg", {"type": "task_complete", "turn_id": "turn"}),
    ]
    if late_result:
        index = next(
            i for i, r in enumerate(rows) if r["type"] == "token_usage_record" and r["payload"]["response_id"] == "b"
        )
        rows.insert(
            index, row(14, "response_item", {"type": "function_call_output", "call_id": "late", "output": "late"})
        )
        rows[-1:] = [
            row(17, "response_item", {"type": "message", "role": "assistant", "content": []}),
            row(
                18,
                "token_usage_record",
                {"turn_id": "turn", "response_id": "c", "usage": {"input_tokens": 20, "output_tokens": 10}},
            ),
            row(19, "event_msg", {"type": "task_complete", "turn_id": "turn"}),
        ]
    turn = _extract_turn_from_rollout(write_rows(tmp_path, rows), "turn")
    assert len(turn["llm_calls"]) == (3 if late_result else 2)
    attrs = turn["llm_calls"][1]["measurements"]
    if late_result:
        assert "llm.response_cycle.duration_ms" not in turn["llm_calls"][2]["measurements"]
    assert ("llm.response_cycle.duration_ms" in attrs) is not interrupt
    if not interrupt:
        assert attrs["llm.response_cycle.duration_ms"] == 5000
    assert attrs["llm.reasoning_effort"] == "high"
    assert attrs["llm.service_tier"] == "priority"
    assert attrs["llm.token_count.completion_details.non_reasoning"] == 60


def test_child_root_does_not_claim_foreground_from_lineage_alone():
    turn = {
        "turn_start_ms": 1000,
        "turn_end_ms": 2000,
        "turn_attribution": {"parent_turn_id": "parent"},
        "status": "completed",
    }
    payload = render_turn("child", "turn", turn, lambda *_: ("openai", "openai"))
    spans = [span for rs in payload["resourceSpans"] for scope in rs["scopeSpans"] for span in scope["spans"]]
    root = next(span for span in spans if not span.get("parentSpanId"))
    attr = next(a for a in root["attributes"] if a["key"] == "agent.execution")
    assert attr["value"]["stringValue"] == "unknown"
