"""Adapt Codex observations to the shared event model and OpenInference spans."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from core.common import build_multi_span, build_span, env, redact_content
from core.event_model import EventGraph, EventStatus, ModelCallEvent, ToolEvent, TurnEvent, Usage
from tracing.codex.hooks.adapter import SCOPE_NAME, SERVICE_NAME
from tracing.codex.hooks.pricing import RATE_DATE, RATE_SOURCE, api_pricing
from tracing.codex.hooks.transcript import text

_MAX_CONTENT_CHARS = 64 * 1024
TOKEN_ATTRS = (
    ("prompt_tokens", "llm.token_count.prompt"),
    ("completion_tokens", "llm.token_count.completion"),
    ("total_tokens", "llm.token_count.total"),
    ("cached_input_tokens", "llm.token_count.prompt_details.cache_read"),
    ("cache_write_input_tokens", "llm.token_count.prompt_details.cache_write"),
    ("reasoning_output_tokens", "llm.token_count.completion_details.reasoning"),
)


def stable_id(*parts: str, size: int = 16) -> str:
    return hashlib.sha256(json.dumps(parts, separators=(",", ":")).encode()).hexdigest()[:size]


def content(enabled: bool, value: object) -> str:
    raw = text(_without_binary(value))
    if not enabled:
        return redact_content(False, raw)
    if len(raw) > _MAX_CONTENT_CHARS:
        return raw[: _MAX_CONTENT_CHARS - 40] + "\n<truncated>"
    return raw


def _without_binary(value: object) -> object:
    if isinstance(value, list):
        return [_without_binary(item) for item in value]
    if isinstance(value, dict):
        kind = value.get("type")
        if isinstance(kind, str) and any(part in kind.lower() for part in ("image", "audio")):
            return {"type": kind, "content": "<binary content omitted>"}
        return {
            key: _without_binary(item)
            for key, item in value.items()
            if key not in ("encrypted_content", "b64_json", "base64", "image_url", "audio_url")
        }
    return value


def status(value: str) -> EventStatus:
    try:
        return EventStatus(value)
    except ValueError:
        return EventStatus.UNKNOWN


def build_graph(thread_id: str, turn_id: str, turn: dict) -> tuple[EventGraph, dict]:
    root_id = f"turn:{thread_id}:{turn_id}"
    graph = EventGraph()
    observations: dict = {}
    start, end = turn.get("turn_start_ms") or None, turn.get("turn_end_ms") or None
    root = TurnEvent(
        root_id,
        thread_id,
        turn_id,
        0,
        start,
        end,
        status(turn.get("status", "unknown")),
        input=turn.get("user_prompt"),
        output=turn.get("assistant_output"),
    )
    graph.events.append(root)
    observations[root_id] = turn
    calls = turn.get("llm_calls") or []
    if "llm_calls" not in turn and (turn.get("token_usage") or turn.get("tool_calls")):
        calls = [
            {
                "start_ts": start,
                "end_ts": end,
                "token_usage": turn.get("token_usage"),
                "tool_calls": turn.get("tool_calls", []),
                "assistant_output": turn.get("assistant_output"),
                "input_parts": [{"kind": "prompt", "value": turn.get("user_prompt")}],
            }
        ]
    tool_ids = {}
    for number, call in enumerate(calls, 1):
        rid = call.get("response_id") or str(number)
        eid = f"response:{thread_id}:{turn_id}:{rid}"
        tokens = call.get("token_usage") or {}
        model_usage = (
            Usage(
                input_tokens=tokens.get("prompt_tokens", 0),
                output_tokens=tokens.get("completion_tokens", 0),
                cache_read_tokens=tokens.get("cached_input_tokens", 0),
                cache_write_tokens=tokens.get("cache_write_input_tokens", 0),
                reported_total_tokens=tokens.get("total_tokens"),
            )
            if tokens
            else None
        )
        graph.events.append(
            ModelCallEvent(
                eid,
                thread_id,
                turn_id,
                len(graph.events),
                call.get("start_ts"),
                call.get("end_ts"),
                status(call.get("status", "unknown")),
                root_id,
                input=call.get("input_parts"),
                output=call.get("assistant_output"),
                source_id=call.get("response_id") or None,
                model=turn.get("model"),
                usage=model_usage,
            )
        )
        observations[eid] = call
        for index, tool in enumerate(call.get("tool_calls") or []):
            cid = tool.get("call_id") or f"{number}:{index}"
            tid = f"tool:{thread_id}:{turn_id}:{cid}"
            if tid in observations:
                continue
            tool_ids[cid] = tid
            graph.events.append(
                ToolEvent(
                    tid,
                    thread_id,
                    turn_id,
                    len(graph.events),
                    tool.get("start_ts"),
                    tool.get("end_ts"),
                    status(tool.get("status", "unknown")),
                    eid,
                    input=tool.get("args"),
                    output=tool.get("output"),
                    tool_call_id=cid,
                    tool_name=tool.get("tool"),
                )
            )
            observations[tid] = tool
    for event in graph.events:
        parent_call = observations[event.event_id].get("parent_call_id")
        if parent_call and parent_call in tool_ids:
            event.parent_event_id = tool_ids[parent_call]
    for index, tool in enumerate(turn.get("tool_calls") or []):
        cid = tool.get("call_id") or f"unassigned:{index}"
        tid = f"tool:{thread_id}:{turn_id}:{cid}"
        if tid not in observations:
            graph.events.append(
                ToolEvent(
                    tid,
                    thread_id,
                    turn_id,
                    len(graph.events),
                    tool.get("start_ts"),
                    tool.get("end_ts"),
                    status(tool.get("status", "unknown")),
                    root_id,
                    input=tool.get("args"),
                    output=tool.get("output"),
                    tool_call_id=cid,
                    tool_name=tool.get("tool"),
                )
            )
            observations[tid] = tool
    graph.validate()
    return graph, observations


def render_turn(thread_id: str, turn_id: str, turn: dict, identity) -> dict:
    graph, observations = build_graph(thread_id, turn_id, turn)
    trace_id = turn.get("trace_id") or stable_id("codex", thread_id, turn_id, size=32)
    project = env.project_name_for(SERVICE_NAME) or "codex"
    cwd = turn.get("cwd") or ""
    llm_system, llm_provider = identity(turn.get("model"), turn.get("model_provider"))
    payloads = []
    for event in graph.events:
        obs = observations[event.event_id]
        attrs: dict = {
            "session.id": thread_id,
            "codex.thread_id": thread_id,
            "codex.turn_id": turn_id,
            "project.name": project,
            "codex.status": event.status.value,
        }
        if env.get_user_id(SERVICE_NAME):
            attrs["user.id"] = env.get_user_id(SERVICE_NAME)
        if cwd:
            attrs["codex.cwd"] = cwd[:4096]
            attrs["codex.workspace"] = Path(cwd).name[:4096]
        if isinstance(event, TurnEvent):
            kind, name = "CHAIN", f"Turn {turn.get('trace_count') or 1}"
            attrs.update(
                {
                    "trace.number": str(turn.get("trace_count") or 1),
                    "input.value": content(env.log_prompts, event.input),
                    "output.value": content(env.log_prompts, event.output),
                }
            )
            if turn.get("diagnostics"):
                attrs["codex.diagnostics"] = json.dumps(turn["diagnostics"])
            # Identity and messages retain current main's indexed attributes.
            attrs["llm.system"] = llm_system
            if llm_provider:
                attrs["llm.provider"] = llm_provider
            for prefix, role, value in (
                ("llm.input_messages", "user", attrs["input.value"]),
                ("llm.output_messages", "assistant", attrs["output.value"]),
            ):
                if value:
                    attrs[f"{prefix}.0.message.role"] = role
                    attrs[f"{prefix}.0.message.content"] = value
            for source, target in (
                ("permission_mode", "codex.approval_mode"),
                ("sandbox_mode", "codex.sandbox_mode"),
                ("duration_ms", "codex.turn.duration_ms"),
            ):
                if turn.get(source) is not None and turn.get(source) != "":
                    attrs[target] = turn[source]
        elif isinstance(event, ModelCallEvent):
            kind, name = "LLM", f"LLM response: {event.source_id or event.sequence}"
            parts = event.input or []
            attrs["input.value"] = content(
                True,
                "\n".join(
                    content(env.log_tool_content if p.get("kind") == "tool_output" else env.log_prompts, p.get("value"))
                    for p in parts
                ),
            )
            attrs["output.value"] = content(env.log_prompts, event.output)
            attrs["codex.timing_source"] = "persisted_response_items"
            attrs["llm.system"] = llm_system
            if llm_provider:
                attrs["llm.provider"] = llm_provider
            if event.model:
                attrs["llm.model_name"] = event.model
                attrs["codex.model_name"] = event.model
            tier = obs.get("service_tier", turn.get("service_tier", ""))
            if tier:
                attrs["codex.service_tier"] = tier
            pricing = api_pricing(event.model or "", tier, obs.get("token_usage") or {})
            if pricing:
                # Phoenix selects pricing by model name, without service-tier
                # predicates. Keep the native model separately and label the SKU.
                attrs["llm.model_name"] = pricing["billing_model"]
                attrs["api_cost.basis"] = "published_api_equivalent"
                attrs["api_cost.currency"] = "USD"
                attrs["api_cost.estimated_usd"] = pricing["cost_usd"]
                attrs["api_cost.rate_date"] = RATE_DATE
                attrs["api_cost.rate_source"] = RATE_SOURCE
                attrs["api_cost.service_tier_assumed"] = not bool(tier)
            elif event.model == "gpt-6.1-sol" and tier not in (
                "",
                "default",
                "standard",
                "priority",
                "fast",
                "flex",
                "ultrafast",
            ):
                # Built-in prices can match substrings; exclude the model identifier.
                attrs["llm.model_name"] = "unpriced Codex service tier"
                attrs["api_cost.basis"] = "unpriced_service_tier"
            elif event.model == "gpt-6.1-sol":
                attrs["llm.model_name"] = "unpriced Codex usage"
                attrs["api_cost.basis"] = "unpriced_usage"
            if event.source_id:
                attrs["llm.message.id"] = event.source_id
            for source, target in TOKEN_ATTRS:
                if type((obs.get("token_usage") or {}).get(source)) is int:
                    attrs[target] = obs["token_usage"][source]
            if obs.get("token_usage"):
                attrs["codex.token_usage"] = json.dumps(obs["token_usage"])
            for index, part in enumerate(parts):
                tool_result = part.get("kind") == "tool_output"
                prefix = f"llm.input_messages.{index}.message"
                attrs[f"{prefix}.role"] = "tool" if tool_result else "user"
                attrs[f"{prefix}.content"] = content(
                    env.log_tool_content if tool_result else env.log_prompts, part.get("value")
                )
                if part.get("call_id"):
                    attrs[f"{prefix}.tool_call_id"] = part["call_id"]
            if attrs["output.value"]:
                attrs["llm.output_messages.0.message.role"] = "assistant"
                attrs["llm.output_messages.0.message.content"] = attrs["output.value"]
            originated = [tool for tool in obs.get("tool_calls", []) if not tool.get("parent_call_id")]
            for index, tool in enumerate(originated):
                prefix = f"llm.output_messages.0.message.tool_calls.{index}.tool_call"
                attrs["llm.output_messages.0.message.role"] = "assistant"
                attrs[f"{prefix}.id"] = tool.get("call_id") or ""
                attrs[f"{prefix}.function.name"] = tool.get("tool") or "unknown_tool"
                attrs[f"{prefix}.function.arguments"] = content(env.log_tool_details, tool.get("args"))
        else:
            assert isinstance(event, ToolEvent)
            kind, name = "TOOL", event.tool_name or "unknown_tool"
            if obs.get("container"):
                kind = "CHAIN"
            attrs.update(
                {
                    "tool.name": name,
                    "tool.id": event.tool_call_id,
                    "tool.call.id": event.tool_call_id,
                    "codex.tool.call_id": event.tool_call_id,
                    "input.value": content(env.log_tool_details, event.input),
                    "output.value": content(env.log_tool_content, event.output),
                    "codex.timing_source": obs.get("timing_source", "persisted_item"),
                }
            )
            args = event.input
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if isinstance(args, dict):
                for key, target in (
                    ("command", "tool.command"),
                    ("cmd", "tool.command"),
                    ("cwd", "tool.cwd"),
                    ("url", "tool.url"),
                    ("query", "tool.query"),
                ):
                    if key in args:
                        attrs[target] = content(env.log_tool_details, args[key])
            if type(obs.get("exit_code")) is int:
                attrs["tool.exit_code"] = obs["exit_code"]
            if isinstance(obs.get("duration"), dict):
                attrs["codex.tool.observed_duration"] = json.dumps(obs["duration"])
        attrs["openinference.span.kind"] = kind
        parent = stable_id(event.parent_event_id) if event.parent_event_id else turn.get("parent_span_id", "")
        code = (
            1
            if event.status == EventStatus.COMPLETED
            else 2 if event.status in (EventStatus.FAILED, EventStatus.CANCELLED) else 0
        )
        payloads.append(
            build_span(
                name,
                kind,
                stable_id(event.event_id),
                trace_id,
                parent,
                event.started_at_ms or 0,
                event.ended_at_ms or event.started_at_ms or 0,
                attrs,
                SERVICE_NAME,
                SCOPE_NAME,
                status_code=code,
            )
        )
    return build_multi_span(payloads, SERVICE_NAME, SCOPE_NAME)
