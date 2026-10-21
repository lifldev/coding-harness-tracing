"""Synthetic regressions for out-of-order and interrupted Claude agent lifecycles."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from unittest.mock import patch

import pytest

from core.common import StateManager
from core.event_model import EventStatus, ModelCallEvent, ToolEvent, TurnEvent
from tracing.claude_code.hooks import handlers
from tracing.claude_code.hooks.span_renderer import render_event_graph
from tracing.claude_code.hooks.transcript import parse_claude_transcript


def assistant(key, content, **extra):
    return {
        "type": "assistant",
        "uuid": key,
        "timestamp": 2000,
        "message": {
            "role": "assistant",
            "id": key,
            "model": "synthetic-model",
            "content": content,
            "usage": {"input_tokens": 2, "output_tokens": 3},
            **extra,
        },
    }


def launch(agent, call, *, result=True):
    rows = [
        assistant(
            f"model-{call}", [{"type": "tool_use", "id": call, "name": "Agent", "input": {"prompt": "synthetic"}}]
        )
    ]
    if result:
        rows.append(
            {
                "type": "user",
                "timestamp": 2100,
                "toolUseResult": {"isAsync": True, "status": "async_launched", "agentId": agent},
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": call, "content": "launched"}],
                },
            }
        )
    return rows


def transcript(tmp_path, name, rows, *, parent_call=None):
    path = tmp_path / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    if parent_call:
        path.with_suffix(".meta.json").write_text(json.dumps({"toolUseId": parent_call, "requestShape": "background"}))
    return path


def spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def attrs(span):
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


@contextmanager
def session(tmp_path, main, send=None):
    state = StateManager(tmp_path, tmp_path / "state.json", tmp_path / "state.lock")
    state.init_state()
    for key, value in {
        "session_id": "synthetic",
        "current_trace_id": "a" * 32,
        "current_trace_span_id": "b" * 16,
        "current_trace_start_time": "1000",
        "trace_count": "1",
        "trace_start_line": "0",
    }.items():
        state.set(key, value)
    sent = []

    def capture(payload):
        sent.append(payload)
        return send(payload) if send else True

    with (
        patch.object(handlers, "resolve_session", return_value=state),
        patch.object(handlers, "resolve_transcript_path", return_value=main),
        patch.object(handlers, "get_timestamp_ms", return_value=3000),
        patch.object(handlers, "send_span", side_effect=capture),
    ):
        yield state, sent


def stop_agent(main, agent, path, **extra):
    handlers._handle_subagent_stop(
        {
            "transcript_path": str(main),
            "agent_id": agent,
            "agent_transcript_path": str(path),
            "agent_type": "synthetic",
            **extra,
        }
    )


def test_nested_agents_match_metadata_even_without_launch_results_and_in_reverse_order(tmp_path):
    main = transcript(tmp_path, "main", launch("outer", "call-outer", result=False))
    outer = transcript(tmp_path, "agent-outer", launch("inner", "call-inner", result=False), parent_call="call-outer")
    inner = transcript(
        tmp_path,
        "agent-inner",
        [assistant("inner-model", [{"type": "text", "text": "done"}])],
        parent_call="call-inner",
    )
    with session(tmp_path, main) as (state, sent):
        stop_agent(main, "inner", inner)
        stop_agent(main, "outer", outer)
        handlers._handle_stop({"transcript_path": str(main)})
    tree = spans(sent[0])
    tools = {attrs(s).get("tool.call.id"): s for s in tree if attrs(s).get("tool.call.id")}
    agents = {attrs(s).get("subagent.id"): s for s in tree if attrs(s).get("openinference.span.kind") == "AGENT"}
    assert set(agents) == {"outer", "inner"}
    assert agents["outer"]["parentSpanId"] == tools["call-outer"]["spanId"]
    assert agents["inner"]["parentSpanId"] == tools["call-inner"]["spanId"]
    assert state.get("pending_subagents") is None


def test_nested_background_child_can_stop_before_parent_after_launch_turn(tmp_path):
    main = transcript(tmp_path, "main", launch("outer", "call-outer"))
    outer = transcript(tmp_path, "agent-outer", launch("inner", "call-inner"))
    inner = transcript(tmp_path, "agent-inner", [assistant("inner-model", [])])
    with session(tmp_path, main) as (state, sent):
        handlers._handle_stop({"transcript_path": str(main)})
        stop_agent(main, "inner", inner)
        stop_agent(main, "outer", outer)
        assert state.get("pending_subagents") is None
        assert state.get("async_subagents") is None
    tree = [s for p in sent for s in spans(p)]
    tools = {attrs(s).get("tool.call.id"): s for s in tree if attrs(s).get("tool.call.id")}
    agents = {attrs(s).get("subagent.id"): s for s in tree if attrs(s).get("openinference.span.kind") == "AGENT"}
    assert set(agents) == {"outer", "inner"}
    assert agents["inner"]["parentSpanId"] == tools["call-inner"]["spanId"]
    assert {s["traceId"] for s in tree} == {"a" * 32}


def test_async_retry_reuses_all_span_ids_and_repeated_stop_does_not_attach_to_new_turn(tmp_path):
    main = transcript(tmp_path, "main", launch("outer", "call-outer"))
    outer = transcript(tmp_path, "agent-outer", [assistant("outer-model", [])])
    attempts = iter([True, False, True])
    with session(tmp_path, main, lambda _: next(attempts)) as (state, sent):
        handlers._handle_stop({"transcript_path": str(main)})
        stop_agent(main, "outer", outer)
        stop_agent(main, "outer", outer)
        state.set("current_trace_id", "c" * 32)
        stop_agent(main, "outer", outer)
        assert state.get("pending_subagents") is None
    assert len(sent) == 3
    assert [s["spanId"] for s in spans(sent[1])] == [s["spanId"] for s in spans(sent[2])]


def test_concurrent_duplicate_stops_export_one_turn(tmp_path):
    main = transcript(tmp_path, "main", [assistant("main-model", [])])
    entered, release = Event(), Event()

    def send(_):
        entered.set()
        assert release.wait(3)
        return True

    with session(tmp_path, main, send) as (_, sent), ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(handlers._handle_stop, {"transcript_path": str(main)})
        assert entered.wait(3)
        second = pool.submit(handlers._handle_stop, {"transcript_path": str(main)})
        release.set()
        first.result()
        second.result()
    assert len(sent) == 1


@pytest.mark.parametrize("outcome", ["cancelled", "failed"])
def test_subagent_outcome_is_preserved(tmp_path, outcome):
    main = transcript(tmp_path, "main", launch("outer", "call-outer"))
    outer = transcript(
        tmp_path,
        "agent-outer",
        [assistant("outer-model", [{"type": "tool_use", "id": "pending", "name": "Read", "input": {}}])],
    )
    with session(tmp_path, main) as (_, sent):
        handlers._handle_stop({"transcript_path": str(main)})
        stop_agent(main, "outer", outer, status=outcome, error="synthetic interruption")
    agent, _, tool = spans(sent[1])
    assert attrs(agent)["event.status"] == outcome
    assert agent["status"]["code"] != 1
    assert tool["status"]["code"] != 1


def test_stop_failure_keeps_chain_and_recorded_children(tmp_path):
    main = transcript(
        tmp_path,
        "main",
        [assistant("main-model", [{"type": "tool_use", "id": "pending", "name": "Read", "input": {}}])],
    )
    with session(tmp_path, main) as (_, sent):
        handlers._handle_stop_failure(
            {"transcript_path": str(main), "error": "interrupted", "error_details": "synthetic"}
        )
    tree = spans(sent[0])
    assert [attrs(s)["openinference.span.kind"] for s in tree] == ["CHAIN", "LLM", "TOOL"]
    assert tree[0]["status"]["code"] == 2


def test_model_timing_provenance_and_final_cache_snapshot(tmp_path):
    first = assistant("model", [], usage={"input_tokens": 2, "output_tokens": 3, "cache_read_input_tokens": 40})
    last = assistant("model", [], usage={"input_tokens": 2, "output_tokens": 3, "cache_read_input_tokens": 50})
    last["uuid"] = "continuation"
    main = transcript(tmp_path, "main", [first, last])
    root = TurnEvent("turn", "session", "1", 0, 1000, 3000, EventStatus.COMPLETED)
    graph = parse_claude_transcript(main, root)
    model = next(e for e in graph.events if isinstance(e, ModelCallEvent))
    assert model.usage.cache_read_tokens == 50
    payload = render_event_graph(graph, trace_id="a" * 32)
    details = attrs(spans(payload)[1])
    assert details["llm.timing.start_source"] == "inferred_previous_conversation_record"
    assert details["llm.timing.end_source"] == "observed_assistant_record"


def test_api_error_and_cancelled_tool_result_are_not_successful(tmp_path):
    error = assistant("api-error", [{"type": "text", "text": "synthetic failure"}])
    error.update(isApiErrorMessage=True, error="overloaded_error")
    result = {
        "type": "user",
        "timestamp": 2500,
        "toolUseResult": {"status": "cancelled"},
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call", "content": "cancelled"}],
        },
    }
    main = transcript(tmp_path, "main", launch("unused", "call", result=False) + [result, error])
    root = TurnEvent("turn", "session", "1", 0, 1000, 3000, EventStatus.COMPLETED)
    graph = parse_claude_transcript(main, root)
    assert next(e for e in graph.events if isinstance(e, ToolEvent)).status is EventStatus.CANCELLED
    assert next(e for e in graph.events if e.event_id == "api-error").status is EventStatus.FAILED


def test_late_metadata_only_agent_keeps_launch_parent_across_session_end(tmp_path):
    main = transcript(tmp_path, "main", launch("outer", "call-outer", result=False))
    outer = transcript(tmp_path, "agent-outer", [assistant("outer-model", [])], parent_call="call-outer")
    with session(tmp_path, main) as (state, sent), patch.object(handlers, "gc_stale_state_files"):
        handlers._handle_stop({"transcript_path": str(main)})
        handlers._handle_session_end({})
        assert state.state_file.is_file()
        stop_agent(main, "outer", outer)
    assert len(sent) == 2
    assert spans(sent[1])[0]["parentSpanId"] == spans(sent[0])[2]["spanId"]


def test_missing_async_transcript_does_not_fall_back_to_unrelated_turn(tmp_path):
    main = transcript(tmp_path, "main", launch("outer", "call-outer"))
    with session(tmp_path, main) as (state, sent):
        handlers._handle_stop({"transcript_path": str(main)})
        state.set("current_trace_id", "c" * 32)
        state.set("current_trace_span_id", "d" * 16)
        stop_agent(main, "outer", tmp_path / "absent.jsonl")
        assert state.get("async_subagents") is not None
    assert len(sent) == 1


def test_concurrent_agents_and_duplicate_completion_keep_individual_parents(tmp_path):
    main = transcript(tmp_path, "main", launch("one", "call-one") + launch("two", "call-two"))
    children = {
        name: transcript(tmp_path, f"agent-{name}", [assistant(f"model-{name}", [])]) for name in ("one", "two")
    }
    with session(tmp_path, main) as (_, sent):
        handlers._handle_stop({"transcript_path": str(main)})
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda name: stop_agent(main, name, children[name]), ["two", "one", "two", "one"]))
    assert len(sent) == 3
    tools = {attrs(s)["tool.call.id"]: s for s in spans(sent[0]) if "tool.call.id" in attrs(s)}
    for payload in sent[1:]:
        agent = spans(payload)[0]
        assert agent["parentSpanId"] == tools[f"call-{attrs(agent)['subagent.id']}"]["spanId"]
