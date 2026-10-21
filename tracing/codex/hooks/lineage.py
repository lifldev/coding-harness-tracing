"""Native Codex lineage verified against child metadata and turn attribution."""

from __future__ import annotations

from pathlib import Path

from core.common import build_span
from tracing.codex.hooks.adapter import SCOPE_NAME, SERVICE_NAME
from tracing.codex.hooks.graph import stable_id
from tracing.codex.hooks.transcript import _extract_turn_from_rollout, own_rows, records, session_metadata

_MAX_DEPTH = 8
_MAX_CHILD_TURNS = 64


def native_parent(meta: dict) -> str:
    source = meta.get("source")
    if not isinstance(source, dict):
        return ""
    subagent = source.get("subagent")
    spawn = subagent.get("thread_spawn") if isinstance(subagent, dict) else None
    parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    return parent if isinstance(parent, str) else ""


def context(thread_id: str, turn_id: str, turn: dict, find, visited=()) -> tuple[str, list]:
    local_trace = stable_id("codex", thread_id, turn_id, size=32)
    meta = turn.get("session_meta") or {}
    parent = native_parent(meta)
    attribution = turn.get("turn_attribution") or {}
    parent_turn = attribution.get("parent_turn_id")
    if not parent:
        return local_trace, []
    if not isinstance(parent_turn, str) or not parent_turn:
        turn.setdefault("diagnostics", []).append("missing_parent_turn_attribution")
        return local_trace, []
    if thread_id in visited or parent in (*visited, thread_id) or len(visited) >= _MAX_DEPTH:
        turn.setdefault("diagnostics", []).append("lineage_cycle_or_depth_limit")
        return local_trace, []
    path = find(parent)
    parent_data = _extract_turn_from_rollout(path, parent_turn) if path else None
    if parent_data is None:
        turn.setdefault("diagnostics", []).append("parent_turn_unavailable")
        return local_trace, []
    parent_meta = parent_data.get("session_meta") or {}
    if parent_meta.get("id") != parent:
        turn.setdefault("diagnostics", []).append("parent_metadata_mismatch")
        return local_trace, []
    trace, _ = context(parent, parent_turn, parent_data, find, (*visited, thread_id))
    if "lineage_cycle_or_depth_limit" in parent_data.get("diagnostics", []):
        turn.setdefault("diagnostics", []).append("lineage_cycle_or_depth_limit")
        return local_trace, []
    agent_id = stable_id(f"agent:{thread_id}:{turn_id}")
    parent_span_id = stable_id(f"turn:{parent}:{parent_turn}")
    turn["parent_span_id"] = agent_id
    turn["trace_id"] = trace
    attrs = {
        "openinference.span.kind": "AGENT",
        "session.id": thread_id,
        "subagent.id": thread_id,
        "codex.parent_thread_id": parent,
        "codex.parent_turn_id": parent_turn,
        "codex.lineage_source": "session_meta.source.subagent.thread_spawn+turn_attribution",
    }
    agent = build_span(
        "Codex subagent",
        "AGENT",
        agent_id,
        trace,
        parent_span_id,
        turn.get("turn_start_ms") or 0,
        turn.get("turn_end_ms") or 0,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=0,
    )
    # Ancestors are emitted by their own turn export; emitting them with a child's
    # end time would invent the parent's duration and overwrite better evidence.
    return trace, [agent]


def child_turns(path: Path, parent_thread: str, parent_turn: str) -> list[str]:
    rows = records(path, [])
    meta = session_metadata(rows)
    if native_parent(meta) != parent_thread:
        return []
    result = []
    for row in own_rows(rows, meta):
        p = row["payload"]
        if row.get("type") != "event_msg" or p.get("type") not in ("task_started", "turn_started"):
            continue
        attr = p.get("turn_attribution")
        if isinstance(attr, dict) and attr.get("parent_turn_id") == parent_turn and isinstance(p.get("turn_id"), str):
            result.append(p["turn_id"])
        if len(result) >= _MAX_CHILD_TURNS:
            break
    return result
