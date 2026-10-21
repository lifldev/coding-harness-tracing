"""Bounded, fail-soft reconstruction of persisted Codex observations.

The regular-file reader is adapted from upstream PR #114. Response timestamps
are observed persistence boundaries, never fabricated provider request times.
"""

from __future__ import annotations

import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

# Reading is streamed, but hard budgets keep notify latency and memory bounded
# for malformed, very large, or concurrently growing rollout files.
_MAX_ROLLOUT_BYTES = 64 << 20  # 64 MiB
_MAX_ROLLOUT_LINE_BYTES = 1 << 20  # 1 MiB
_MAX_ROLLOUT_RECORDS = 200_000


# ---------------------------------------------------------------------------
# Rollout extraction
# ---------------------------------------------------------------------------


@contextmanager
def _open_regular_text(path: Path) -> Iterator[Iterator[str]]:
    """Yield bounded UTF-8 lines from one verified regular-file descriptor."""
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise OSError("rollout candidate is not a regular file")
        if file_stat.st_size > _MAX_ROLLOUT_BYTES:
            raise OSError("rollout exceeds safe byte limit")

        handle = os.fdopen(descriptor, "rb")
        descriptor = -1  # fdopen owns and closes it from here.

        def bounded_lines() -> Iterator[str]:
            total_bytes = 0
            records = 0
            while True:
                raw_line = handle.readline(_MAX_ROLLOUT_LINE_BYTES + 1)
                if not raw_line:
                    return
                total_bytes += len(raw_line)
                records += 1
                if len(raw_line) > _MAX_ROLLOUT_LINE_BYTES:
                    raise OSError("rollout line exceeds safe byte limit")
                if total_bytes > _MAX_ROLLOUT_BYTES:
                    raise OSError("rollout exceeds safe byte limit")
                if records > _MAX_ROLLOUT_RECORDS:
                    raise OSError("rollout exceeds safe record limit")
                yield raw_line.decode("utf-8")

        with handle:
            yield bounded_lines()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _iso_to_ms(ts: object) -> int:
    """Convert an ISO-8601 timestamp string to milliseconds, or return zero."""
    if not isinstance(ts, str) or not ts:
        return 0
    try:
        normalized = ts.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError, OverflowError, OSError):
        return 0


def text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def count(value: object) -> bool:
    return type(value) is int and value >= 0


def usage(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    result = {}
    for source, target in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
        ("cached_input_tokens", "cached_input_tokens"),
        ("cache_write_input_tokens", "cache_write_input_tokens"),
        ("reasoning_output_tokens", "reasoning_output_tokens"),
    ):
        if count(raw.get(source)):
            result[target] = raw[source]
    if "prompt_tokens" in result and "cached_input_tokens" in result:
        result["non_cached_input_tokens"] = max(result["prompt_tokens"] - result["cached_input_tokens"], 0)
    return result


def records(path: Path, diagnostics: list) -> list:
    result = []
    try:
        with _open_regular_text(path) as handle:
            for line in handle:
                try:
                    obj = json.loads(line)
                except (ValueError, TypeError):
                    diagnostics.append("malformed_record")
                    continue
                if not isinstance(obj, dict) or not isinstance(obj.get("payload"), dict):
                    diagnostics.append("malformed_record")
                    continue
                result.append(obj)
    except (OSError, UnicodeError):
        diagnostics.append("rollout_read_failed")
    return result


def session_metadata(rows: list) -> dict:
    # Forks prepend their own metadata, followed by copied parent history.
    return next((r["payload"] for r in rows if r.get("type") == "session_meta"), {})


def own_rows(rows: list, meta: dict) -> list:
    boundary = meta.get("subagent_history_start_ordinal")
    if count(boundary):
        return [r for r in rows if count(r.get("ordinal")) and r["ordinal"] >= boundary]
    # Old fork formats without a boundary cannot safely separate inherited history.
    if meta.get("forked_from_id"):
        return []
    return rows


def message(payload: dict) -> str:
    content = payload.get("content", [])
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))


def _extract_turn_from_rollout(rollout_path: Path, turn_id: str) -> "dict | None":
    if not isinstance(turn_id, str) or not turn_id:
        return None
    diagnostics: list = []
    rows = records(rollout_path, diagnostics)
    meta = session_metadata(rows)
    rows = own_rows(rows, meta)
    selected = []
    trace_count = 0
    started = False
    for row in rows:
        p = row["payload"]
        if row.get("type") == "event_msg" and p.get("type") in ("task_started", "turn_started"):
            if started:
                break
            trace_count += 1
            started = p.get("turn_id") == turn_id
        if started:
            selected.append(row)
            if row.get("type") == "event_msg" and p.get("type") in ("task_complete", "task_completed", "turn_aborted"):
                if p.get("turn_id") in (None, "", turn_id):
                    break
    if not selected:
        return None
    modern = any(r.get("type") == "token_usage_record" and r["payload"].get("turn_id") == turn_id for r in selected)
    turn: dict = {
        "trace_count": trace_count,
        "turn_start_ms": 0,
        "turn_end_ms": 0,
        "duration_ms": None,
        "user_prompt": "",
        "assistant_output": "",
        "model": "",
        "cwd": "",
        "permission_mode": "",
        "sandbox_mode": "",
        "model_provider": meta.get("model_provider"),
        "token_usage": None,
        "tool_calls": [],
        "llm_calls": [],
        "diagnostics": diagnostics,
        "status": "unknown",
        "session_meta": meta,
        "turn_attribution": {},
    }
    tools: list = turn["tool_calls"]
    calls: list = turn["llm_calls"]
    pending: dict = {}
    seen_items: set = set()
    seen_usage: dict = {}
    previous_total = None
    active: dict | None = None
    search_ends: dict = {}
    inputs: list = []
    open_wrappers: list = []

    def response(ts: int) -> dict:
        nonlocal active
        if active is None:
            active = {
                "start_ts": ts,
                "end_ts": ts,
                "input_parts": list(inputs),
                "assistant_output": "",
                "token_usage": None,
                "tool_calls": [],
                "response_id": "",
                "status": "unknown",
            }
            inputs.clear()
        active["end_ts"] = max(active["end_ts"], ts)
        return active

    def close_response(ts: int, tokens: dict, response_id: str = "") -> None:
        nonlocal active
        call = response(ts)
        call.update(
            token_usage=tokens or None, response_id=response_id, status="completed", end_ts=max(ts, call["end_ts"])
        )
        calls.append(call)
        active = None

    def add_tool(name: str, args: object, cid: str, ts: int, output=None, owner=None) -> dict:
        if cid and cid in pending:
            return pending[cid]
        call = owner if owner is not None else response(ts)
        entry = {
            "tool": name,
            "args": args,
            "output": output,
            "call_id": cid,
            "start_ts": ts,
            "end_ts": ts,
            "status": "unknown",
            "decision": None,
            "response": call,
            "timing_source": "persisted_item",
        }
        tools.append(entry)
        if call:
            call["tool_calls"].append(entry)
        if cid:
            pending[cid] = entry
        return entry

    entry: dict | None
    for row in selected:
        outer, p = row.get("type"), row["payload"]
        kind = p.get("type")
        ts = _iso_to_ms(row.get("timestamp"))
        turn["turn_end_ms"] = max(turn["turn_end_ms"], ts)
        if p.get("turn_id") not in (None, "", turn_id):
            continue
        if outer == "turn_context":
            for src, dst in (("model", "model"), ("cwd", "cwd"), ("approval_policy", "permission_mode")):
                if isinstance(p.get(src), str):
                    turn[dst] = p[src]
            sandbox = p.get("sandbox_policy")
            if isinstance(sandbox, dict):
                turn["sandbox_mode"] = sandbox.get("type", "")
        elif outer == "token_usage_record":
            rid = p.get("response_id")
            tokens = usage(p.get("usage"))
            if not isinstance(rid, str) or not rid or not tokens:
                diagnostics.append("invalid_response_usage")
                continue
            if rid in seen_usage:
                if seen_usage[rid] != tokens:
                    diagnostics.append("conflicting_response_usage")
                continue
            seen_usage[rid] = tokens
            close_response(ts, tokens, rid)
        elif outer == "event_msg":
            if kind in ("task_started", "turn_started"):
                start = p.get("started_at")
                turn["turn_start_ms"] = start * 1000 if count(start) else ts
                attr = p.get("turn_attribution")
                turn["turn_attribution"] = attr if isinstance(attr, dict) else {}
            elif kind in ("task_complete", "task_completed"):
                turn["status"] = "completed"
                if isinstance(p.get("last_agent_message"), str):
                    turn["assistant_output"] = p["last_agent_message"]
                if count(p.get("completed_at")):
                    turn["turn_end_ms"] = p["completed_at"] * 1000
                if count(p.get("duration_ms")):
                    turn["duration_ms"] = p["duration_ms"]
            elif kind in ("turn_aborted", "task_interrupted", "task_failed"):
                turn["status"] = "failed" if kind == "task_failed" else "cancelled"
            elif kind == "user_message" and isinstance(p.get("message"), str):
                turn["user_prompt"] = p["message"]
                if not inputs or inputs[-1] != {"kind": "prompt", "value": p["message"]}:
                    inputs.append({"kind": "prompt", "value": p["message"]})
            elif kind == "agent_message" and isinstance(p.get("message"), str):
                turn["assistant_output"] = p["message"]
                response(ts)["assistant_output"] = p["message"]
            elif kind == "token_count" and not modern:
                info = p.get("info")
                if not isinstance(info, dict):
                    continue
                total = info.get("total_token_usage")
                if isinstance(total, dict) and previous_total == total:
                    continue
                previous_total = total
                tokens = usage(info.get("last_token_usage"))
                if tokens:
                    close_response(ts, tokens)
            elif kind == "web_search_end":
                cid = p.get("call_id")
                if isinstance(cid, str):
                    search_ends[cid] = (ts, p)
            elif kind in ("item_started", "item_completed"):
                item = p.get("item")
                if not isinstance(item, dict):
                    diagnostics.append("malformed_item")
                    continue
                itype = item.get("type")
                cid = item.get("id")
                if not isinstance(cid, str) or not cid:
                    diagnostics.append("missing_item_id")
                    continue
                if itype == "CommandExecution":
                    entry = pending.get(cid)
                    if entry is None:
                        wrapper = open_wrappers[-1] if len(open_wrappers) == 1 else None
                        direct = []
                        for candidate in tools:
                            if (
                                candidate["tool"].rsplit(".", 1)[-1] != "exec_command"
                                or candidate["output"] is not None
                            ):
                                continue
                            args = candidate["args"]
                            if isinstance(args, str):
                                try:
                                    args = json.loads(args)
                                except ValueError:
                                    args = None
                            if isinstance(args, dict) and args.get("cmd", args.get("command")) == item.get("command"):
                                direct.append(candidate)
                        if len(direct) == 1 and wrapper is None:
                            entry = direct[0]
                            pending[cid] = entry
                        else:
                            owner = wrapper["response"] if wrapper else False
                            entry = add_tool(
                                "exec_command",
                                {k: item[k] for k in ("command", "cwd", "parsed_cmd") if k in item},
                                cid,
                                ts,
                                owner=owner,
                            )
                            if wrapper is None:
                                diagnostics.append("command_parent_unobserved")
                        if wrapper:
                            entry["parent_call_id"] = wrapper["call_id"]
                    start, end = p.get("started_at_ms"), p.get("completed_at_ms")
                    if count(start):
                        entry["start_ts"] = start
                        entry["timing_source"] = "item_lifecycle"
                    if count(end):
                        entry["end_ts"] = end
                    elif kind == "item_completed":
                        entry["end_ts"] = ts
                    if kind == "item_completed":
                        entry["output"] = item.get("aggregated_output")
                        entry["exit_code"] = item.get("exit_code")
                        entry["duration"] = item.get("duration")
                        entry["status"] = (
                            "failed"
                            if type(item.get("exit_code")) is int and item["exit_code"] != 0
                            else _status(item.get("status"))
                        )
                        if entry["status"] == "completed" and type(item.get("exit_code")) is not int:
                            entry["status"] = "unknown"
                            diagnostics.append("command_result_unknown")
                elif itype == "SubAgentActivity":
                    # Interactions/notifications are not evidence of a new invocation.
                    if item.get("kind") == "spawned" and isinstance(item.get("agent_thread_id"), str):
                        turn.setdefault("child_thread_ids", []).append(item["agent_thread_id"])
        elif outer == "response_item":
            cid = p.get("call_id") or p.get("id") or ""
            if not isinstance(cid, str):
                diagnostics.append("invalid_call_id")
                continue
            if kind in (
                "function_call",
                "custom_tool_call",
                "local_shell_call",
                "tool_search_call",
                "image_generation_call",
                "web_search_call",
            ):
                item_key = (kind, cid) if cid else (kind, row.get("ordinal"), ts)
                if item_key in seen_items:
                    continue
                seen_items.add(item_key)
                name, args = p.get("name") or kind.removesuffix("_call"), p.get("arguments", p.get("input", ""))
                if not isinstance(name, str):
                    name = "unknown_tool"
                if kind == "web_search_call":
                    action = p.get("action") if isinstance(p.get("action"), dict) else {}
                    name = {"open_page": "open_page", "find_in_page": "find_in_page", "search": "web_search"}.get(
                        action.get("type") or "", "web_search_other"
                    )
                    args = action.get("url") if name == "open_page" else action.get("query", action)
                if kind == "local_shell_call":
                    args = p.get("action")
                if kind == "tool_search_call":
                    args = {"arguments": p.get("arguments"), "execution": p.get("execution")}
                if kind == "image_generation_call":
                    args = p.get("revised_prompt", "")
                entry = add_tool(name, args, cid, ts)
                if kind == "custom_tool_call" and name.rsplit(".", 1)[-1] == "exec":
                    entry["container"] = True
                    open_wrappers.append(entry)
                if kind in ("web_search_call", "image_generation_call", "local_shell_call"):
                    entry["status"] = _status(p.get("status"))
                    entry["output"] = p.get("status")
                    if kind == "image_generation_call" and p.get("result"):
                        entry["output"] = "<image result omitted>"
                    if cid in search_ends:
                        entry["end_ts"] = max(ts, search_ends[cid][0])
            elif kind in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
                entry = pending.get(cid)
                output = p.get("tools") if kind == "tool_search_output" else p.get("output")
                if entry is not None:
                    if entry["timing_source"] != "item_lifecycle":
                        entry["output"] = output
                        entry["end_ts"] = max(entry["start_ts"], ts)
                    entry["status"] = (
                        "failed"
                        if type(entry.get("exit_code")) is int and entry["exit_code"] != 0
                        else _output_status(output)
                    )
                    if entry in open_wrappers:
                        open_wrappers.remove(entry)
                    if entry["tool"].rsplit(".", 1)[-1] == "spawn_agent":
                        result = output
                        if isinstance(result, str):
                            try:
                                result = json.loads(result)
                            except ValueError:
                                result = None
                        if isinstance(result, dict) and isinstance(result.get("agent_id"), str):
                            turn.setdefault("child_thread_ids", []).append(result["agent_id"])
                else:
                    diagnostics.append("orphan_tool_output")
                inputs.append({"kind": "tool_output", "value": output, "call_id": cid})
            elif kind in ("message", "agent_message"):
                msg = message(p)
                if p.get("role") == "user":
                    turn["user_prompt"] = msg
                    part = {"kind": "prompt", "value": msg}
                    if not inputs or inputs[-1] != part:
                        inputs.append(part)
                elif p.get("role") == "assistant" or kind == "agent_message":
                    call = response(ts)
                    if msg:
                        if call["assistant_output"] != msg:
                            call["assistant_output"] += ("\n" if call["assistant_output"] else "") + msg
                        turn["assistant_output"] = msg
            elif kind == "reasoning":
                response(ts)
    if active is not None:
        # Persisted response items are observations, even without usage/completion.
        calls.append(active)
    if turn["status"] == "unknown":
        diagnostics.append("missing_completion")
    if turn["status"] == "completed" and any(e["status"] == "unknown" for e in tools):
        diagnostics.append("incomplete_tool_result")
        turn["status"] = "unknown"
    if turn["status"] != "completed":
        for call in calls:
            if call["status"] == "unknown":
                call["status"] = turn["status"]
    sums: dict = {}
    for call in calls:
        for k, v in (call["token_usage"] or {}).items():
            sums[k] = sums.get(k, 0) + v
    if sums:
        if "prompt_tokens" in sums and "cached_input_tokens" in sums:
            sums["non_cached_input_tokens"] = max(sums["prompt_tokens"] - sums["cached_input_tokens"], 0)
        sums["model"] = turn["model"]
        turn["token_usage"] = sums
    for entry in tools:
        entry.pop("response", None)
        if entry["end_ts"] < entry["start_ts"]:
            diagnostics.append("invalid_tool_timing")
            entry["end_ts"] = entry["start_ts"]
    turn["diagnostics"] = sorted(set(diagnostics))
    return turn


def _status(value: object) -> str:
    if value in ("completed", "success", "succeeded"):
        return "completed"
    if value in ("failed", "error", "declined", "denied"):
        return "failed"
    if value in ("cancelled", "interrupted", "aborted"):
        return "cancelled"
    return "unknown"


def _output_status(value: object) -> str:
    if isinstance(value, list):
        return "failed" if any(_output_status(item) == "failed" for item in value) else "completed"
    if isinstance(value, dict):
        if value.get("isError") is True or value.get("error"):
            return "failed"
        if type(value.get("exit_code")) is int and value["exit_code"] != 0:
            return "failed"
        if value.get("type") == "text":
            return _output_status(value.get("text"))
    if isinstance(value, str):
        try:
            structured = json.loads(value)
        except ValueError:
            structured = None
        if isinstance(structured, (dict, list)):
            return _output_status(structured)
        # Unified-exec's result envelope is an observed result, not source code.
        exit_match = re.search(r"(?m)^Process exited with code (-?\d+)\s*$", value)
        if exit_match and int(exit_match[1]) != 0:
            return "failed"
    # A persisted output proves completion, but text is not a structured error signal.
    return "completed" if value is not None else "unknown"
