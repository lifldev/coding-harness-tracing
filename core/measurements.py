"""Observed measurements shared by exporters; never fabricate provider timing."""

import math


def valid_count(value):
    return type(value) is int and value >= 0


def response_measurements(effort=None, tier=None, output=None, reasoning=None, start=None, end=None, end_source=None):
    attrs = {}
    if isinstance(effort, str) and effort in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        attrs["llm.reasoning_effort"] = effort
    if isinstance(tier, str) and tier in {"standard", "default", "priority", "fast", "flex", "ultrafast"}:
        attrs["llm.service_tier"] = tier
    if valid_count(output) and valid_count(reasoning) and reasoning <= output:
        attrs["llm.token_count.completion_details.reasoning"] = reasoning
        attrs["llm.token_count.completion_details.non_reasoning"] = output - reasoning
    if (
        valid_count(output)
        and all(type(value) in (int, float) and math.isfinite(value) and value >= 0 for value in (start, end))
        and end > start
    ):
        attrs.update(
            {
                "llm.response_cycle.duration_ms": end - start,
                "llm.response_cycle.start_source": "observed_tool_result",
                "llm.response_cycle.end_source": end_source,
            }
        )
    return attrs


def trigger_measurements(prompt):
    if not isinstance(prompt, str) or not prompt.strip():
        return {"turn.trigger": "unknown", "turn.trigger_source": "unavailable"}
    prompt = prompt.lstrip()
    trigger = "human"
    if prompt.startswith("<task-notification>"):
        trigger = "task_notification"
    elif prompt.startswith("You have ") and "orchestration message" in prompt[:100]:
        trigger = "orchestration_notification"
    elif prompt.startswith(("Stop hook feedback:", "Goal check-in:")):
        trigger = "goal_check"
    elif prompt.startswith(("This session is being continued", "Orca restarted", "Base directory for this skill:")):
        trigger = "continuation"
    return {"turn.trigger": trigger, "turn.trigger_source": "transcript_marker_estimate"}
