"""Synthetic rollout regressions; no captured user content or backend calls."""

import json
from unittest.mock import patch

from tracing.codex.hooks.handlers import _build_and_send_spans, _extract_turn_from_rollout


def record(kind, payload, second=0, **extra):
    return {"type": kind, "timestamp": f"2026-10-21T00:00:{second:02d}Z", "payload": payload, **extra}


def write(tmp_path, records, session="s"):
    path = tmp_path / f"rollout-test-{session}.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n")
    return path


def attrs(span):
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


def spans(turn):
    sent = []
    with patch("tracing.codex.hooks.handlers.send_span_to_backend", side_effect=lambda p: sent.append(p) or True):
        _build_and_send_spans("s", "t", turn)
    return sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]


def modern_records():
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 80,
        "output_tokens": 5,
        "reasoning_output_tokens": 2,
        "cache_write_input_tokens": 0,
        "total_tokens": 105,
    }
    return [
        record("session_meta", {"id": "s", "model_provider": "openai"}),
        record("event_msg", {"type": "task_started", "turn_id": "t"}),
        record("event_msg", {"type": "user_message", "message": "inspect a file"}),
        record("response_item", {"type": "custom_tool_call", "name": "exec", "call_id": "c", "input": "tool code"}, 1),
        record("token_usage_record", {"turn_id": "t", "response_id": "r1", "usage": usage}, 2),
        record(
            "event_msg",
            {
                "type": "item_completed",
                "turn_id": "t",
                "started_at_ms": 1792540803100,
                "completed_at_ms": 1792540803200,
                "item": {
                    "type": "CommandExecution",
                    "id": "exec-1",
                    "command": "cat missing",
                    "cwd": "/workspace",
                    "parsed_cmd": [],
                    "aggregated_output": "missing file",
                    "status": "completed",
                    "exit_code": 1,
                    "duration": {"secs": 0, "nanos": 100000000},
                },
            },
            4,
        ),
        record(
            "response_item",
            {"type": "custom_tool_call_output", "call_id": "c", "output": [{"type": "text", "text": "result"}]},
            4,
        ),
        record(
            "event_msg", {"type": "token_count", "info": {"last_token_usage": usage, "total_token_usage": usage}}, 4
        ),
        record("token_usage_record", {"turn_id": "t", "response_id": "r1", "usage": usage}, 4),
        record(
            "response_item",
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
            5,
        ),
        record("token_usage_record", {"turn_id": "t", "response_id": "r2", "usage": usage}, 6),
        record("event_msg", {"type": "task_complete", "turn_id": "t", "last_agent_message": "done"}, 7),
    ]


def test_response_usage_is_exact_and_not_duplicated(tmp_path):
    turn = _extract_turn_from_rollout(write(tmp_path, modern_records()), "t")
    assert len(turn["llm_calls"]) == 2
    assert turn["token_usage"]["total_tokens"] == 210
    output = spans(turn)
    assert attrs(output[0])["openinference.span.kind"] == "CHAIN"
    assert "llm.token_count.total" not in attrs(output[0])
    llms = [s for s in output if attrs(s)["openinference.span.kind"] == "LLM"]
    assert [attrs(s)["llm.token_count.total"] for s in llms] == [105, 105]
    assert attrs(llms[0])["llm.token_count.prompt_details.cache_read"] == 80
    assert attrs(llms[0])["llm.token_count.completion_details.reasoning"] == 2
    assert output == spans(turn)


def test_structured_commands_are_observed_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
    turn = _extract_turn_from_rollout(write(tmp_path, modern_records()), "t")
    command = next(e for e in turn["tool_calls"] if e["call_id"] == "exec-1")
    assert command["start_ts"] == 1792540803100
    assert command["end_ts"] == 1792540803200
    assert command["exit_code"] == 1
    output = spans(turn)
    command_span = next(s for s in output if attrs(s).get("tool.id") == "exec-1")
    wrapper = next(s for s in output if attrs(s).get("tool.id") == "c")
    assert command_span["parentSpanId"] == wrapper["spanId"]
    assert command_span["status"]["code"] == 2
    assert attrs(command_span)["tool.command"] == "cat missing"
    assert attrs(command_span)["output.value"] == "missing file"
    assert json.loads(attrs(wrapper)["output.value"]) == [{"text": "result", "type": "text"}]


def test_mismatched_completion_is_not_success(tmp_path):
    rows = modern_records()[:4] + [record("event_msg", {"type": "task_complete", "turn_id": "other"}, 7)]
    turn = _extract_turn_from_rollout(write(tmp_path, rows), "t")
    assert turn["status"] == "unknown"
    assert "missing_completion" in turn["diagnostics"]
    assert spans(turn)[0]["status"]["code"] == 0


def test_privacy_applies_to_tool_results_in_following_response(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
    turn = _extract_turn_from_rollout(write(tmp_path, modern_records()), "t")
    emitted = json.dumps(spans(turn))
    assert "inspect a file" in emitted
    assert "cat missing" not in emitted
    assert "tool code" not in emitted
    assert "missing file" not in emitted
    assert '"text": "result"' not in emitted
    assert "<redacted" in emitted


def test_builtin_tools_preserve_actions_and_omit_binary(tmp_path, monkeypatch):
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
    rows = modern_records()[:3] + [
        record(
            "response_item",
            {
                "type": "web_search_call",
                "id": "w1",
                "status": "completed",
                "action": {"type": "find_in_page", "url": "https://example.test", "pattern": "word"},
            },
            1,
        ),
        record(
            "response_item",
            {
                "type": "web_search_call",
                "id": "w2",
                "status": "completed",
                "action": {"type": "search", "queries": ["a", "b"]},
            },
            2,
        ),
        record(
            "response_item",
            {
                "type": "image_generation_call",
                "id": "img",
                "status": "completed",
                "revised_prompt": "a diagram",
                "result": "PRIVATE_IMAGE_BYTES",
            },
            3,
        ),
        record(
            "response_item", {"type": "tool_search_call", "call_id": "discovery", "arguments": {"query": "lookup"}}, 4
        ),
        record(
            "response_item", {"type": "tool_search_output", "call_id": "discovery", "tools": [{"name": "lookup"}]}, 5
        ),
        record("event_msg", {"type": "task_complete", "turn_id": "t"}, 6),
    ]
    turn = _extract_turn_from_rollout(write(tmp_path, rows), "t")
    output = spans(turn)
    by_name = {attrs(s).get("tool.name"): attrs(s) for s in output if attrs(s).get("tool.name")}
    assert json.loads(by_name["find_in_page"]["input.value"])["pattern"] == "word"
    assert json.loads(by_name["web_search"]["input.value"])["queries"] == ["a", "b"]
    assert json.loads(by_name["tool_search"]["output.value"]) == [{"name": "lookup"}]
    assert "PRIVATE_IMAGE_BYTES" not in json.dumps(output)


def test_no_execution_inferred_from_javascript(tmp_path):
    rows = modern_records()[:4] + [record("event_msg", {"type": "task_complete", "turn_id": "t"}, 5)]
    rows[3]["payload"]["input"] = 'await tools.exec_command({cmd:"never ran"})'
    turn = _extract_turn_from_rollout(write(tmp_path, rows), "t")
    assert [e["tool"] for e in turn["tool_calls"]] == ["exec"]
    assert turn["status"] == "unknown"
    assert "incomplete_tool_result" in turn["diagnostics"]


def test_duplicate_and_failed_delivery_receipts(tmp_path):
    from tracing.codex.hooks.delivery import deliver
    from tracing.codex.hooks.graph import render_turn
    from tracing.codex.hooks.handlers import _llm_identity

    turn = _extract_turn_from_rollout(write(tmp_path, modern_records()), "t")
    payload = render_turn("s", "t", turn, _llm_identity)
    sent = []

    def fail(p):
        sent.append(p)
        return False

    assert not deliver(payload, fail, tmp_path / "receipts")
    assert not list((tmp_path / "receipts").glob("*.json"))

    def succeed(p):
        sent.append(p)
        return True

    assert deliver(payload, succeed, tmp_path / "receipts")
    assert deliver(payload, succeed, tmp_path / "receipts")
    assert len(sent) == 2
    assert sent[0] == sent[1]
    receipts = "".join(p.read_text() for p in (tmp_path / "receipts").glob("*.json"))
    assert "inspect a file" not in receipts
    assert "result" not in receipts


def test_dry_run_does_not_record_delivery(tmp_path, monkeypatch):
    from tracing.codex.hooks.delivery import deliver
    from tracing.codex.hooks.graph import render_turn
    from tracing.codex.hooks.handlers import _llm_identity

    monkeypatch.setenv("ARIZE_DRY_RUN", "true")
    turn = _extract_turn_from_rollout(write(tmp_path, modern_records()), "t")
    assert deliver(render_turn("s", "t", turn, _llm_identity), lambda _: True, tmp_path / "receipts")
    assert not (tmp_path / "receipts").exists()


def test_malformed_records_and_unknown_usage_fail_soft(tmp_path):
    rows = modern_records()[:3] + [
        record("turn_context", {"turn_id": "t", "sandbox_policy": []}),
        record(
            "token_usage_record",
            {"turn_id": "t", "response_id": "r", "usage": {"input_tokens": True, "output_tokens": -1}},
        ),
        record("event_msg", {"type": "item_completed", "turn_id": "t", "item": []}),
        record("event_msg", {"type": "turn_aborted", "turn_id": "t"}, 7),
    ]
    path = write(tmp_path, rows)
    path.write_text("null\n[]\ninvalid\n" + path.read_text())
    turn = _extract_turn_from_rollout(path, "t")
    assert turn["status"] == "cancelled"
    assert turn["token_usage"] is None
    assert set(turn["diagnostics"]) >= {"malformed_record", "invalid_response_usage", "malformed_item"}
    assert spans(turn)[0]["status"]["code"] == 2


def test_standalone_command_has_no_invented_response_parent(tmp_path):
    rows = [modern_records()[1], modern_records()[5], modern_records()[-1]]
    turn = _extract_turn_from_rollout(write(tmp_path, rows), "t")
    assert turn["llm_calls"] == []
    from tracing.codex.hooks.graph import build_graph

    graph, _ = build_graph("s", "t", turn)
    # A command alone cannot establish an LLM response.
    assert len(graph.events) == 2
    assert graph.events[1].parent_event_id == graph.events[0].event_id
    assert "command_parent_unobserved" in turn["diagnostics"]


def native_rows(session, turn, parent=None, parent_turn=None, spawn=None):
    meta = {"id": session, "model_provider": "openai"}
    attr = {}
    if parent:
        meta.update(
            source={"subagent": {"thread_spawn": {"parent_thread_id": parent}}},
            forked_from_id=parent,
            subagent_history_start_ordinal=10,
        )
        attr["parent_turn_id"] = parent_turn
    rows = [record("session_meta", meta)]
    if parent:
        # This copied parent turn must never become a child trace.
        rows += [
            record("session_meta", {"id": parent}),
            record("event_msg", {"type": "task_started", "turn_id": "inherited"}, ordinal=1),
            record("event_msg", {"type": "task_complete", "turn_id": "inherited"}, ordinal=2),
        ]
    rows.append(record("event_msg", {"type": "task_started", "turn_id": turn, "turn_attribution": attr}, ordinal=10))
    if spawn:
        rows += [
            record(
                "response_item",
                {"type": "function_call", "name": "spawn_agent", "call_id": "spawn", "arguments": "{}"},
                1,
                ordinal=11,
            ),
            record(
                "response_item",
                {"type": "function_call_output", "call_id": "spawn", "output": {"agent_id": spawn}},
                2,
                ordinal=12,
            ),
        ]
    rows += [
        record(
            "response_item",
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "complete"}]},
            3,
            ordinal=13,
        ),
        record(
            "token_usage_record",
            {
                "turn_id": turn,
                "response_id": f"r-{turn}",
                "usage": {"input_tokens": 4, "output_tokens": 1, "total_tokens": 5},
            },
            4,
            ordinal=14,
        ),
        record("event_msg", {"type": "task_complete", "turn_id": turn}, 5, ordinal=15),
    ]
    return rows


def test_nested_child_lineage_and_duplicate_notify(tmp_path, monkeypatch):
    from tracing.codex.hooks import handlers
    from tracing.codex.hooks.graph import stable_id
    from tracing.codex.hooks.lineage import child_turns

    monkeypatch.setattr("tracing.codex.hooks.delivery._STATE_DIR", tmp_path / "receipts")
    parent_path = write(tmp_path, native_rows("parent", "pt", spawn="child"), "parent")
    child_path = write(tmp_path, native_rows("child", "ct", "parent", "pt", spawn="grandchild"), "child")
    grand_path = write(tmp_path, native_rows("grandchild", "gt", "child", "ct"), "grandchild")
    paths = {"parent": parent_path, "child": child_path, "grandchild": grand_path}
    monkeypatch.setattr(handlers, "_find_rollout_file", paths.get)
    assert _extract_turn_from_rollout(child_path, "inherited") is None
    assert child_turns(child_path, "parent", "pt") == ["ct"]
    sent = []
    monkeypatch.setattr(handlers, "send_span_to_backend", lambda payload: sent.append(payload) or True)
    handlers._handle_notify({"type": "agent-turn-complete", "thread-id": "parent", "turn-id": "pt"})
    assert len(sent) == 3
    all_spans = [s for p in sent for s in p["resourceSpans"][0]["scopeSpans"][0]["spans"]]
    assert len({s["traceId"] for s in all_spans}) == 1
    by_id = {s["spanId"]: s for s in all_spans}
    child_turn = by_id[stable_id("turn:child:ct")]
    child_agent = by_id[child_turn["parentSpanId"]]
    assert attrs(child_agent)["openinference.span.kind"] == "AGENT"
    assert child_agent["parentSpanId"] == stable_id("turn:parent:pt")
    grand_turn = by_id[stable_id("turn:grandchild:gt")]
    assert by_id[grand_turn["parentSpanId"]]["parentSpanId"] == child_turn["spanId"]
    assert sum(attrs(s).get("llm.token_count.total", 0) for s in all_spans) == 15
    handlers._handle_notify({"type": "agent-turn-complete", "thread-id": "child", "turn-id": "ct"})
    handlers._handle_notify({"type": "agent-turn-complete", "thread-id": "parent", "turn-id": "pt"})
    assert len(sent) == 3


def test_parent_missing_is_diagnostic_and_never_invented(tmp_path):
    from tracing.codex.hooks.lineage import context

    turn = _extract_turn_from_rollout(write(tmp_path, native_rows("child", "ct", "absent", "pt")), "ct")
    _, agents = context("child", "ct", turn, lambda _: None)
    assert not agents
    assert "parent_span_id" not in turn
    assert "parent_turn_unavailable" in turn["diagnostics"]


def test_background_child_can_finish_after_parent(tmp_path, monkeypatch):
    from tracing.codex.hooks import handlers

    monkeypatch.setattr("tracing.codex.hooks.delivery._STATE_DIR", tmp_path / "receipts")
    p = write(tmp_path, native_rows("parent", "pt", spawn="child"), "parent")
    child_rows = native_rows("child", "ct", "parent", "pt")
    c = write(tmp_path, child_rows[:-1], "child")
    monkeypatch.setattr(handlers, "_find_rollout_file", {"parent": p, "child": c}.get)
    sent = []
    monkeypatch.setattr(handlers, "send_span_to_backend", lambda payload: sent.append(payload) or True)
    handlers._handle_notify({"type": "agent-turn-complete", "thread-id": "parent", "turn-id": "pt"})
    before = sent[-1]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    write(tmp_path, child_rows, "child")
    handlers._handle_notify({"type": "agent-turn-complete", "thread-id": "child", "turn-id": "ct"})
    after = sent[-1]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len(sent) == 3
    before_root = next(s for s in before if attrs(s).get("openinference.span.kind") == "CHAIN")
    after_root = next(s for s in after if attrs(s).get("openinference.span.kind") == "CHAIN")
    assert before_root["spanId"] == after_root["spanId"]
    assert before_root["status"]["code"] == 0
    assert after_root["status"]["code"] == 1


def test_reader_rejects_symlinks_and_enforces_limits(tmp_path, monkeypatch):
    import pytest

    from tracing.codex.hooks import transcript

    target = write(tmp_path, modern_records())
    alias = tmp_path / "alias.jsonl"
    alias.symlink_to(target)
    with pytest.raises(OSError):
        with transcript._open_regular_text(alias) as handle:
            list(handle)
    monkeypatch.setattr(transcript, "_MAX_ROLLOUT_BYTES", 10)
    with pytest.raises(OSError):
        with transcript._open_regular_text(target) as handle:
            list(handle)
    monkeypatch.setattr(transcript, "_MAX_ROLLOUT_BYTES", 1 << 20)
    monkeypatch.setattr(transcript, "_MAX_ROLLOUT_LINE_BYTES", 10)
    with pytest.raises(OSError):
        with transcript._open_regular_text(target) as handle:
            list(handle)


def test_direct_command_observations_do_not_duplicate_tool(tmp_path):
    rows = modern_records()
    rows[3]["payload"] = {
        "type": "function_call",
        "name": "exec_command",
        "call_id": "c",
        "arguments": '{"cmd":"cat missing"}',
    }
    rows[6]["payload"] = {
        "type": "function_call_output",
        "call_id": "c",
        "output": "Process exited with code 1\nmissing file",
    }
    turn = _extract_turn_from_rollout(write(tmp_path, rows), "t")
    assert len(turn["tool_calls"]) == 1
    assert turn["tool_calls"][0]["status"] == "failed"
    assert turn["tool_calls"][0]["start_ts"] == 1792540803100
