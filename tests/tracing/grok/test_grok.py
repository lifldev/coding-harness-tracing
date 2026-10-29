"""Native Grok fixtures contain synthetic records only."""

import io
import json

import pytest

from tracing.grok import install
from tracing.grok.hooks import handlers


@pytest.fixture
def session(tmp_path):
    root = tmp_path / "sessions"
    directory = root / "workspace" / "session-1"
    directory.mkdir(parents=True)
    (directory / "summary.json").write_text(
        json.dumps({"info": {"id": "session-1", "cwd": str(tmp_path)}, "reasoning_effort": "high"})
    )
    events = [
        {"type": "turn_started", "ts": "2026-10-30T00:00:00Z"},
        {
            "type": "tool_completed",
            "ts": "2026-10-30T00:00:02Z",
            "tool_call_id": "tool-1",
            "duration_ms": 100,
            "outcome": "success",
            "tool_name": "read_file",
        },
        {"type": "turn_ended", "ts": "2026-10-30T00:00:04Z", "outcome": "completed"},
    ]
    (directory / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events))
    updates = []

    def update(offset, **value):
        updates.append(
            {
                "params": {
                    "sessionId": "session-1",
                    "_meta": {"agentTimestampMs": 1793318400000 + offset},
                    "update": value,
                }
            }
        )

    update(0, sessionUpdate="user_message_chunk", content={"type": "text", "text": "private prompt"})
    update(1000, sessionUpdate="tool_call", toolCallId="tool-1", title="read_file", rawInput={"path": "private-path"})
    update(
        2000,
        sessionUpdate="tool_call_update",
        toolCallId="tool-1",
        status="completed",
        rawOutput={"text": "private tool result"},
    )
    update(3000, sessionUpdate="agent_message_chunk", content={"type": "text", "text": "private response"})
    update(
        4000,
        sessionUpdate="turn_completed",
        prompt_id="prompt-1",
        stop_reason="end_turn",
        elapsed_ms=4000,
        usage={
            "inputTokens": 120,
            "cachedReadTokens": 100,
            "outputTokens": 20,
            "reasoningTokens": 5,
            "totalTokens": 140,
            "modelCalls": 2,
            "apiDurationMs": 2500,
            "modelUsage": {
                "grok-4.7-build": {
                    "inputTokens": 120,
                    "cachedReadTokens": 100,
                    "outputTokens": 20,
                    "reasoningTokens": 5,
                    "totalTokens": 140,
                }
            },
        },
    )
    (directory / "updates.jsonl").write_text("\n".join(json.dumps(e) for e in updates))
    return root, directory, updates


def spans(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"]


def attrs(span):
    return {a["key"]: next(iter(a["value"].values())) for a in span["attributes"]}


def test_native_turn_and_tool_accounting(session):
    root, _, _ = session
    payload = handlers.reconstruct("session-1", root)[0]
    chain, model, tool = spans(payload)
    assert attrs(chain)["openinference.span.kind"] == "CHAIN"
    assert attrs(model)["llm.token_count.prompt"] == 120
    assert attrs(model)["llm.token_count.total"] == 140
    assert attrs(model)["grok.usage.aggregation"] == "turn_model"
    assert attrs(model)["llm.token_count.completion_details.reasoning"] == 5
    assert model["startTimeUnixNano"] == model["endTimeUnixNano"]
    assert "llm.response_cycle.duration_ms" not in attrs(model)
    assert "llm.token_count.prompt" not in attrs(chain)
    assert tool["parentSpanId"] == chain["spanId"]
    assert int(tool["endTimeUnixNano"]) - int(tool["startTimeUnixNano"]) == 100_000_000
    assert attrs(chain)["grok.stop_reason"] == "end_turn"
    assert attrs(chain)["grok.session.reasoning_effort"] == "high"
    assert "llm.reasoning_effort" not in attrs(chain)


def test_privacy_gates_and_no_encrypted_reasoning(session, monkeypatch):
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
    data = json.dumps(handlers.reconstruct("session-1", session[0]))
    assert "private prompt" not in data
    assert "private response" not in data
    assert "private-path" not in data
    assert "private tool result" not in data


def test_multiturn_cancellation_and_incomplete_tail(session):
    root, directory, updates = session
    second = json.loads(json.dumps(updates))
    for row in second:
        row["params"]["_meta"]["agentTimestampMs"] += 10000
        u = row["params"]["update"]
        if "toolCallId" in u:
            u["toolCallId"] = "tool-2"
        if u.get("sessionUpdate") == "turn_completed":
            u["prompt_id"] = "prompt-2"
            u["stop_reason"] = "max_turns"
    tail = {
        "params": {
            "sessionId": "session-1",
            "update": {"sessionUpdate": "user_message_chunk", "content": {"type": "text", "text": "unfinished"}},
        }
    }
    (directory / "updates.jsonl").write_text("\n".join(json.dumps(e) for e in updates + second + [tail]))
    payloads = handlers.reconstruct("session-1", root)
    assert len(payloads) == 2
    assert spans(payloads[0])[0]["traceId"] != spans(payloads[1])[0]["traceId"]
    assert spans(payloads[1])[0]["status"]["code"] == 2
    assert "unfinished" not in json.dumps(payloads)


def test_delivery_dedup_failure_retry_and_dry_run(session, tmp_path, monkeypatch):
    payload = handlers.reconstruct("session-1", session[0])[0]
    state = tmp_path / "delivery"
    calls = []

    def send(value):
        calls.append(value)
        return True

    assert not handlers.deliver(payload, lambda _: False, state)
    assert handlers.deliver(payload, send, state)
    assert handlers.deliver(payload, send, state)
    assert len(calls) == 1
    receipts = list(state.glob("*.json"))
    assert len(receipts) == 1 and "private" not in receipts[0].read_text()
    monkeypatch.setenv("ARIZE_DRY_RUN", "true")
    assert handlers.deliver(payload, send, tmp_path / "dry")
    assert not list((tmp_path / "dry").glob("*.json"))


def test_path_escape_and_symlink_rejected(session, tmp_path):
    root, directory, _ = session
    assert handlers.reconstruct("../session-1", root) == []
    target = directory / "updates.jsonl"
    content = target.read_text()
    target.unlink()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text(content)
    target.symlink_to(elsewhere)
    assert handlers.reconstruct("session-1", root) == []


def test_malformed_and_oversized_records_fail_without_fabrication(session):
    root, directory, _ = session
    (directory / "updates.jsonl").write_text("{broken\n[]\n")
    assert handlers.reconstruct("session-1", root) == []
    (directory / "updates.jsonl").write_text("x" * ((1 << 20) + 1))
    assert handlers.reconstruct("session-1", root) == []


def test_duplicate_completion_and_model_split_do_not_duplicate_tokens(session):
    root, directory, updates = session
    usage = updates[-1]["params"]["update"]["usage"]
    usage["modelUsage"]["other-model"] = {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}
    (directory / "updates.jsonl").write_text("\n".join(json.dumps(e) for e in updates + [updates[-1]]))
    payloads = handlers.reconstruct("session-1", root)
    assert len(payloads) == 1
    models = [attrs(s) for s in spans(payloads[0]) if attrs(s)["openinference.span.kind"] == "LLM"]
    assert len(models) == 2
    assert sum(m["llm.token_count.prompt"] for m in models) == 130


def test_long_sessions_keep_newest_closed_turns(session):
    root, directory, updates = session
    history = []
    for index in range(65):
        turn = json.loads(json.dumps(updates))
        for row in turn:
            row["params"]["_meta"]["agentTimestampMs"] += index * 10000
        turn[-1]["params"]["update"]["prompt_id"] = f"prompt-{index}"
        history.extend(turn)
    (directory / "updates.jsonl").write_text("\n".join(json.dumps(row) for row in history))
    payloads = handlers.reconstruct("session-1", root)
    assert len(payloads) == 64
    assert attrs(spans(payloads[0])[0])["grok.prompt_id"] == "prompt-1"
    assert attrs(spans(payloads[-1])[0])["grok.prompt_id"] == "prompt-64"


def test_tool_failure_status_does_not_fabricate_task_failure(session):
    root, directory, _ = session
    p = directory / "events.jsonl"
    p.write_text(p.read_text().replace('"outcome": "success"', '"outcome": "failed"'))
    chain, _, tool = spans(handlers.reconstruct("session-1", root)[0])
    assert tool["status"]["code"] == 2
    assert chain["status"]["code"] == 1


def test_metadata_mismatch_and_directory_symlink_rejected(session, tmp_path):
    root, directory, _ = session
    p = directory / "summary.json"
    p.write_text(p.read_text().replace('"session-1"', '"different-session"'))
    assert handlers.reconstruct("session-1", root) == []
    other = tmp_path / "redirected"
    directory.rename(other)
    directory.symlink_to(other, target_is_directory=True)
    assert handlers.reconstruct("session-1", root) == []


def test_hook_reads_native_payload_and_is_fail_soft(session, monkeypatch):
    root, _, _ = session
    monkeypatch.setenv("GROK_HOME", str(root.parent))
    monkeypatch.setattr(handlers, "send_span", lambda _: (_ for _ in ()).throw(RuntimeError("offline")))
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"sessionId": "session-1", "hookEventName": "session_end"}))
    )
    handlers.main()


def test_hook_install_is_additive_staged_and_refuses_foreign_file(tmp_path):
    stage = tmp_path / "stage.json"
    target = tmp_path / "hooks" / "arize-tracing.json"
    install.stage_hooks(stage, "/reviewed/runtime/.venv/bin/arize-harness")
    value = json.loads(stage.read_text())
    assert set(value["hooks"]) >= {"Stop", "StopFailure", "StopCancelled", "SessionEnd"}
    install.install_hooks(stage, target)
    assert target.read_text() == stage.read_text()
    target.write_text('{"hooks":{}}')
    with pytest.raises(ValueError, match="owned"):
        install.install_hooks(stage, target)


def test_installer_uses_platform_venv_launcher(tmp_path, monkeypatch):
    from core import config, setup

    monkeypatch.setenv("GROK_HOME", str(tmp_path))
    monkeypatch.setattr(config, "load_config", lambda: {"harnesses": {"grok": {"target": "phoenix"}}})
    executable = tmp_path / "Scripts" / "arize-harness.exe"
    monkeypatch.setattr(setup, "venv_bin", lambda name: executable)
    monkeypatch.setattr("sys.argv", ["install.py", "install"])
    install.main()
    value = json.loads((tmp_path / "hooks" / "arize-tracing.json").read_text())
    assert str(executable) in value["hooks"]["Stop"][0]["hooks"][0]["command"]
