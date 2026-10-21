#!/usr/bin/env python3
"""Codex notify handler -- rollout-JSONL-driven span emission.

The agent-turn-complete callback replays bounded rollout observations as shared
typed events, then renders a CHAIN turn, individual LLM responses and tools.
Delivery receipts suppress duplicate callbacks without storing private content.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from core.common import (
    build_span,
    env,
    error,
    log,
)
from core.common import send_span as send_span_to_backend
from core.constants import MODEL_FAMILY_SYSTEMS
from tracing.codex.constants import get_codex_home
from tracing.codex.hooks.adapter import SCOPE_NAME, SERVICE_NAME, check_requirements, load_env_file
from tracing.codex.hooks.delivery import deliver
from tracing.codex.hooks.graph import content, render_turn, stable_id
from tracing.codex.hooks.lineage import child_turns, context
from tracing.codex.hooks.transcript import _extract_turn_from_rollout, _iso_to_ms, _open_regular_text

# Root of Codex's per-session rollout transcripts.
_CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


# ---------------------------------------------------------------------------
# Payload helpers (notify payload from Codex)
# ---------------------------------------------------------------------------


def _flex_get(d: dict, *keys, default: str = "") -> str:
    """Return the first non-empty value among `keys`, else `default`."""
    for key in keys:
        val = d.get(key)
        if isinstance(val, str) and val:
            return val
    return default


# ---------------------------------------------------------------------------
# Rollout location
# ---------------------------------------------------------------------------


def _find_rollout_file(session_id: str, sessions_root: "Path | None" = None) -> "Path | None":
    """Locate the rollout JSONL file for a given session_id.

    File names embed the session_id, so a filename-pattern match is fast even
    on a deep directory tree.
    """
    if sessions_root is not None:
        root = sessions_root
    elif os.environ.get("CODEX_HOME", ""):
        root = get_codex_home() / "sessions"
    else:
        root = _CODEX_SESSIONS_ROOT
    if not isinstance(session_id, str) or not _SESSION_ID_RE.fullmatch(session_id) or not root.is_dir():
        return None
    try:
        root_resolved = root.resolve()
        for path in root.rglob(f"rollout-*-{session_id}.jsonl"):
            if path.is_symlink() or root_resolved not in path.resolve().parents:
                continue
            with _open_regular_text(path) as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict) or row.get("type") != "session_meta":
                        continue
                    meta = row.get("payload")
                    if not isinstance(meta, dict) or meta.get("id") != session_id:
                        break
                    return path
                else:
                    # Older formats may have no metadata; the literal filename
                    # still identifies the requested session.
                    return path
    except (OSError, UnicodeError, RuntimeError):
        return None
    return None


# ---------------------------------------------------------------------------
# Span assembly
# ---------------------------------------------------------------------------

# o1/o3/o4 only as a whole model-name token (o1, o1-preview, ...), not as a
# substring of an unrelated word.
_O_SERIES_MODEL_RE = re.compile(r"^o[134](\b|-|$)")


def _llm_identity(model: "object", provider: "object") -> "tuple[str, str | None]":
    """Derive OpenInference ``llm.system``/``llm.provider`` for a Codex turn.

    ``llm.system`` = the AI product/model family
    ``llm.provider`` = the hosting provider.
    Different bc DeepSeek models can be hosted on Azure, and other such cases.

    For llm.system, per OpenInference, a well-known value MUST be used when one
    applies:
        1. A model-family match on the model name
        2. Else, the provider, when it is ``openai`` or ``azure``
        3. Else, when the provider is absent, ``"openai"``, which is the default
           provider id when ``session_meta`` omits the field.
        4. Else, the provider id itself as a spec permitted custom value
    """
    model_str = model if isinstance(model, str) else ""
    provider_str = provider if isinstance(provider, str) else None

    model_lower = model_str.lower()
    provider_lower = provider_str.lower() if provider_str else None

    llm_system: "str | None" = None
    model_id = model_lower.rsplit("/", 1)[-1]
    if _O_SERIES_MODEL_RE.match(model_id):
        llm_system = "openai"
    else:
        for substr, system in MODEL_FAMILY_SYSTEMS:
            if substr in model_lower:
                llm_system = system
                break

    if llm_system is None:
        if provider_lower in ("openai", "azure"):
            llm_system = "openai"
        elif provider_lower is None:
            llm_system = "openai"
        else:
            llm_system = provider_lower

    return llm_system, provider_lower


def _put_indexed_message(attrs: dict, prefix: str, role: str, content: str) -> None:
    """Write one OpenInference indexed message attribute pair at index 0."""
    if not content:
        return
    attrs[f"{prefix}.0.message.role"] = role
    attrs[f"{prefix}.0.message.content"] = content


def _build_and_send_spans(thread_id: str, turn_id: str, turn: dict) -> None:
    payload = render_turn(thread_id, turn_id, turn, _llm_identity)
    if not send_span_to_backend(payload):
        error("Failed to send Codex turn")


def _send_legacy_single_span(thread_id: str, turn_id: str, input_json: dict) -> None:
    """Represent unavailable rollout data as an unknown CHAIN observation."""
    assistant_msg = (
        input_json.get("last-assistant-message")
        or input_json.get("last_assistant_message")
        or input_json.get("lastAssistantMessage")
    )
    if isinstance(assistant_msg, dict):
        # Sometimes wrapped: try common keys
        for k in ("text", "message", "content"):
            v = assistant_msg.get(k)
            if isinstance(v, str) and v:
                assistant_msg = v
                break

    user_msgs = input_json.get("input-messages") or input_json.get("input_messages") or ""
    user_prompt = ""
    if isinstance(user_msgs, list):
        for m in reversed(user_msgs):
            if isinstance(m, str) and m:
                user_prompt = m
                break
            if isinstance(m, dict) and m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str) and c:
                    user_prompt = c
                    break
                if isinstance(c, list):
                    for piece in c:
                        if isinstance(piece, dict) and piece.get("text"):
                            user_prompt = piece["text"]
                            break
                    if user_prompt:
                        break
    elif isinstance(user_msgs, str):
        user_prompt = user_msgs

    user_prompt = content(env.log_prompts, user_prompt)
    assistant_output = content(env.log_prompts, assistant_msg if isinstance(assistant_msg, str) else "")
    final_output = assistant_output

    trace_id = stable_id("codex", thread_id, turn_id, size=32)
    span_id = stable_id(f"turn:{thread_id}:{turn_id}")
    now = _iso_to_ms(input_json.get("timestamp"))

    attrs = {
        "session.id": thread_id,
        "project.name": env.project_name_for(SERVICE_NAME) or "codex",
        "openinference.span.kind": "CHAIN",
        "input.value": user_prompt,
        "output.value": final_output,
        "codex.thread_id": thread_id,
        "codex.turn_id": turn_id,
        "codex.notify_fallback": "true",
        "codex.diagnostics": '["rollout_unavailable"]',
        # No rollout here, so the real model/provider is unknown.
        "llm.system": "codex",
    }
    user_id = env.get_user_id(SERVICE_NAME)
    if user_id:
        attrs["user.id"] = user_id
    _put_indexed_message(attrs, "llm.input_messages", "user", user_prompt)
    _put_indexed_message(attrs, "llm.output_messages", "assistant", assistant_output)

    parent_span = build_span(
        "Turn",
        "CHAIN",
        span_id,
        trace_id,
        "",
        now,
        now,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=0,
    )
    if not deliver(parent_span, send_span_to_backend):
        error("Failed to send fallback span to backend")
    else:
        log(f"Turn sent via fallback (thread={thread_id}, turn={turn_id})")


# ---------------------------------------------------------------------------
# Main handler
# ---------------------------------------------------------------------------


def _handle_notify(input_json: dict) -> None:
    """Handle one Codex notify event."""
    if not isinstance(input_json, dict) or input_json.get("type") != "agent-turn-complete":
        return

    thread_id = _flex_get(input_json, "thread-id", "thread_id", "threadId")
    turn_id = _flex_get(input_json, "turn-id", "turn_id", "turnId")

    if not _SESSION_ID_RE.fullmatch(thread_id) or not _SESSION_ID_RE.fullmatch(turn_id):
        error("Codex notify missing or invalid thread/turn identity")
        return

    rollout_path = _find_rollout_file(thread_id)
    if rollout_path is None:
        log(f"notify: no rollout file for session {thread_id}, falling back to single span")
        _send_legacy_single_span(thread_id, turn_id, input_json)
        return

    turn = _extract_turn_from_rollout(rollout_path, turn_id)
    if turn is None:
        log(f"notify: turn {turn_id} not found in {rollout_path}; falling back to single span")
        _send_legacy_single_span(thread_id, turn_id, input_json)
        return

    _export_observed_turn(thread_id, turn_id, turn, set())


def _export_observed_turn(thread_id: str, turn_id: str, turn: dict, visited: set) -> None:
    if (thread_id, turn_id) in visited or len(visited) >= 64:
        return
    visited.add((thread_id, turn_id))
    _, agent_payloads = context(thread_id, turn_id, turn, _find_rollout_file)
    payload = render_turn(thread_id, turn_id, turn, _llm_identity)
    for agent in agent_payloads:
        payload["resourceSpans"][0]["scopeSpans"][0]["spans"].insert(
            0, agent["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        )
    if not deliver(payload, send_span_to_backend):
        error("Failed to deliver observed Codex turn")
    # Child IDs must be observed in structured spawn results/activity, then
    # independently confirmed against native child metadata and attribution.
    for child_id in sorted(set(turn.get("child_thread_ids", [])))[:64]:
        path = _find_rollout_file(child_id)
        if path is None:
            continue
        for child_turn_id in child_turns(path, thread_id, turn_id):
            child = _extract_turn_from_rollout(path, child_turn_id)
            if child is not None:
                _export_observed_turn(child_id, child_turn_id, child, visited)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def notify() -> None:
    """Entry point for ``arize-hook-codex-notify``.

    Codex passes the notify-event JSON on ``sys.argv[1]`` (not stdin) and
    expects no stdout response.
    """
    try:
        load_env_file(get_codex_home() / "arize-env.sh")
        if not check_requirements():
            return
        raw = sys.argv[1] if len(sys.argv) > 1 else "{}"
        input_json = json.loads(raw)
        _handle_notify(input_json)
    except Exception as e:
        error(f"codex notify hook failed: {e}")


if __name__ == "__main__":
    notify()
