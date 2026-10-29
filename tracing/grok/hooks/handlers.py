"""Reconstruct completed native Grok turns without making inference calls."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime
from pathlib import Path

from core.common import build_multi_span, build_span, env, error, redirect_stderr_to_log_file, send_span
from core.constants import STATE_BASE_DIR
from core.delivery import deliver as _deliver
from core.work_context import register_session_cwd

SERVICE = "grok"
SCOPE = "arize-grok-plugin"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_EVENTS = {"stop", "stop_failure", "stop_cancelled", "session_end", "notification"}


def stable_id(*parts, size=16):
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()[:size]


def records(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 64 << 20:
            raise ValueError("invalid native record file")
        total = 0
        for number in range(200001):
            line = handle.readline((1 << 20) + 1)
            if not line:
                return
            total += len(line)
            if number == 200000 or len(line) > 1 << 20 or total > 64 << 20:
                raise ValueError("native record budget exceeded")
            yield line.decode("utf-8")


def rows(path):
    for line in records(path):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            yield value


def iso_ms(value):
    if not isinstance(value, str):
        return 0
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except (ValueError, OverflowError, OSError):
        return 0


def count(value):
    return type(value) is int and value >= 0


def observation_ms(row):
    meta = row.get("params", {}).get("_meta", {})
    value = meta.get("agentTimestampMs") if isinstance(meta, dict) else None
    if count(value):
        return value
    value = row.get("timestamp")
    return value * 1000 if count(value) else 0


def content(enabled, value):
    if not enabled:
        return "[REDACTED]"
    return (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True))[:65536]


def _native_text(update):
    value = update.get("content")
    return value.get("text", "") if isinstance(value, dict) and value.get("type") == "text" else ""


def _render(sid, meta, segment, completion, end, events, previous_end):
    prompt_id = completion.get("prompt_id", "")
    if not isinstance(prompt_id, str) or not _ID.fullmatch(prompt_id) or not end:
        return None
    starts = [
        iso_ms(e.get("ts"))
        for e in events
        if e.get("type") == "turn_started" and previous_end < iso_ms(e.get("ts")) <= end
    ]
    duration = completion.get("elapsed_ms")
    start = max(starts) if starts else end - duration if count(duration) else 0
    if start <= 0 or start > end:
        return None
    trace = stable_id("grok", sid, prompt_id, size=32)
    root = stable_id("grok-turn", sid, prompt_id)
    reason = completion.get("stop_reason")
    status = 1 if reason in ("end_turn", "EndTurn") else 2 if reason else 0
    base = {
        "session.id": sid,
        "grok.prompt_id": prompt_id,
        "grok.source": "native_session_records",
        "llm.system": "xai",
        "llm.provider": "xai",
    }
    # Effort is session metadata, not a claim about a historical resumed turn.
    effort = meta.get("reasoning_effort")
    if effort:
        base["grok.session.reasoning_effort"] = effort
    prompt = "".join(_native_text(u) for _, u in segment if u.get("sessionUpdate") == "user_message_chunk")
    output = "".join(_native_text(u) for _, u in segment if u.get("sessionUpdate") == "agent_message_chunk")
    root_attrs = {
        **base,
        "openinference.span.kind": "CHAIN",
        "grok.stop_reason": reason or "unknown",
        "input.value": content(env.log_prompts, prompt),
        "output.value": content(env.log_prompts, output),
    }
    if count(duration):
        root_attrs["grok.turn.elapsed_ms"] = duration
    payloads = [
        build_span("Grok turn", "CHAIN", root, trace, "", start, end, root_attrs, SERVICE, SCOPE, status_code=status)
    ]
    usage = completion.get("usage")
    if isinstance(usage, dict):
        models = usage.get("modelUsage")
        if not isinstance(models, dict) or not models:
            models = {"unknown": usage}
        for model, values in models.items():
            if not isinstance(values, dict):
                continue
            attrs = {
                **base,
                "openinference.span.kind": "LLM",
                "grok.usage.aggregation": "turn_model",
                "grok.usage.source": "turn_completed.usage",
                "grok.timing.boundary": "accounting_snapshot",
            }
            if model != "unknown":
                attrs["llm.model_name"] = model
            for source, dest in [
                ("inputTokens", "prompt"),
                ("outputTokens", "completion"),
                ("totalTokens", "total"),
                ("cachedReadTokens", "prompt_details.cache_read"),
                ("cacheCreationTokens", "prompt_details.cache_write"),
                ("reasoningTokens", "completion_details.reasoning"),
            ]:
                if count(values.get(source)):
                    attrs["llm.token_count." + dest] = values[source]
            for key in ["modelCalls", "apiDurationMs"]:
                if count(values.get(key)):
                    attrs["grok.usage." + key] = values[key]
            if len(models) == 1:
                for key in ["modelCalls", "apiDurationMs"]:
                    if count(usage.get(key)):
                        attrs["grok.usage." + key] = usage[key]
            # Totals are a zero-duration accounting snapshot, never an invented
            # contiguous inference or response-cycle interval.
            payloads.append(
                build_span(
                    "Grok model usage (turn aggregate)",
                    "LLM",
                    stable_id("grok-usage", sid, prompt_id, model),
                    trace,
                    root,
                    end,
                    end,
                    attrs,
                    SERVICE,
                    SCOPE,
                    status_code=0,
                )
            )
    tools = {}
    for observed, update in segment:
        if update.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
            continue
        cid = update.get("toolCallId")
        if not isinstance(cid, str) or not cid:
            continue
        tool = tools.setdefault(cid, {"first": observed})
        tool.update(update)
        tool["last"] = observed
    for cid, tool in tools.items():
        native = next(
            (
                e
                for e in events
                if e.get("type") == "tool_completed"
                and e.get("tool_call_id") == cid
                and start <= iso_ms(e.get("ts")) <= end
            ),
            {},
        )
        finish = iso_ms(native.get("ts")) or tool.get("last", 0)
        elapsed = native.get("duration_ms")
        begin = finish - elapsed if count(elapsed) else tool["first"]
        if not (start <= begin <= finish <= end):
            continue
        tool_status = native.get("outcome")
        code = (
            1
            if tool_status == "success"
            else 2 if tool_status in ("error", "failure", "failed") or tool.get("status") == "failed" else 0
        )
        info = tool.get("_meta", {}).get("x.ai/tool", {})
        if not isinstance(info, dict):
            info = {}
        name = native.get("tool_name") or info.get("name") or tool.get("title") or "unknown"
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,127}", name):
            name = "unknown"
        attrs = {
            **base,
            "openinference.span.kind": "TOOL",
            "tool.name": name,
            "grok.tool_call_id": cid,
            "grok.tool.status": tool_status or tool.get("status", "unknown"),
            "grok.timing.source": "native_tool_duration" if count(elapsed) else "acp_observation_interval",
            "input.value": content(env.log_tool_details and env.log_tool_content, tool.get("rawInput", {})),
            "output.value": content(env.log_tool_content, tool.get("rawOutput", {})),
        }
        payloads.append(
            build_span(
                name,
                "TOOL",
                stable_id("grok-tool", sid, prompt_id, cid),
                trace,
                root,
                begin,
                finish,
                attrs,
                SERVICE,
                SCOPE,
                status_code=code,
            )
        )
    return build_multi_span(payloads, SERVICE, SCOPE)


def reconstruct(session_id, sessions_root=None):
    """Return bounded closed-turn snapshots; incomplete tails remain retryable."""
    if not isinstance(session_id, str) or not _ID.fullmatch(session_id):
        return []
    root = (
        Path(sessions_root)
        if sessions_root is not None
        else Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok"))) / "sessions"
    )
    try:
        for directory in root.glob("*/" + session_id):
            if (
                any(p.is_symlink() for p in [root, directory.parent, directory])
                or root.resolve() not in directory.resolve().parents
            ):
                continue
            meta = json.loads("".join(records(directory / "summary.json")))
            if not isinstance(meta, dict) or meta.get("info", {}).get("id") != session_id:
                continue
            cwd = meta["info"].get("cwd")
            if isinstance(cwd, str):
                register_session_cwd(SERVICE, session_id, cwd)
            events = list(rows(directory / "events.jsonl")) if (directory / "events.jsonl").exists() else []
            result, segment, seen, previous_end = [], [], set(), 0
            for row in rows(directory / "updates.jsonl"):
                params = row.get("params")
                if not isinstance(params, dict) or params.get("sessionId") != session_id:
                    continue
                update = params.get("update")
                if not isinstance(update, dict):
                    continue
                observed = observation_ms(row)
                if update.get("sessionUpdate") != "turn_completed":
                    if update.get("sessionUpdate") in {
                        "user_message_chunk",
                        "agent_message_chunk",
                        "tool_call",
                        "tool_call_update",
                    }:
                        segment.append((observed, update))
                    continue
                pid = update.get("prompt_id")
                if not isinstance(pid, str) or pid in seen:
                    continue
                payload = _render(session_id, meta, segment, update, observed, events, previous_end)
                if payload:
                    result.append(payload)
                    seen.add(pid)
                    if len(result) > 64:
                        result.pop(0)
                segment = []
                previous_end = observed
            return result
    except (OSError, ValueError, UnicodeError, TypeError, AttributeError, RuntimeError):
        return []
    return []


def deliver(payload, send, state_dir=None):
    return _deliver(payload, send, state_dir or STATE_BASE_DIR / SERVICE / "delivery")


def main():
    """Native hook and explicit saved-session replay entry point; never gate work."""
    try:
        os.environ.setdefault("ARIZE_LOG_FILE", str(STATE_BASE_DIR.parent / "logs" / "grok.log"))
        redirect_stderr_to_log_file()
        if not env.trace_enabled:
            return
        raw = sys.stdin.read((1 << 20) + 1)
        if len(raw) > 1 << 20:
            return
        event = json.loads(raw)
        if not isinstance(event, dict) or event.get("hookEventName") not in _EVENTS:
            return
        for payload in reconstruct(event.get("sessionId")):
            if not deliver(payload, send_span):
                error("Grok trace delivery failed; saved session remains replayable")
    except Exception:
        error("Grok trace hook failed; saved session remains replayable")
