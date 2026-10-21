"""API-equivalent billing selection uses individual response usage."""

import re

import pytest

from tests.tracing.codex.test_fidelity import attrs, modern_records, record, spans, write
from tracing.codex.hooks.handlers import _extract_turn_from_rollout


def test_observed_priority_survives_turn_selection_and_preserves_native_model(tmp_path):
    rows = modern_records()
    rows.insert(
        1, record("event_msg", {"type": "thread_settings_applied", "thread_settings": {"service_tier": "priority"}})
    )
    rows.insert(3, record("turn_context", {"model": "gpt-6.1-sol", "turn_id": "t"}))
    turn = _extract_turn_from_rollout(write(tmp_path, rows), "t")
    llms = [attrs(s) for s in spans(turn) if attrs(s)["openinference.span.kind"] == "LLM"]
    assert len(llms) == 2
    assert {a["codex.model_name"] for a in llms} == {"gpt-6.1-sol"}
    assert {a["llm.model_name"] for a in llms} == {"gpt-6.1-sol [priority]"}
    assert {a["codex.service_tier"] for a in llms} == {"priority"}
    assert all(a["api_cost.basis"] == "published_api_equivalent" for a in llms)


@pytest.mark.parametrize(
    "tier,multiplier", [("default", 1), ("priority", 2), ("fast", 2), ("flex", 0.5), ("ultrafast", 6)]
)
def test_context_boundary_cache_and_reasoning_accounting(tier, multiplier):
    from tracing.codex.hooks.pricing import api_pricing

    # Cached input is included in input, and reasoning is included in output.
    usage = {
        "prompt_tokens": 272000,
        "cached_input_tokens": 200000,
        "cache_write_input_tokens": 10000,
        "completion_tokens": 1000,
        "reasoning_output_tokens": 400,
    }
    short = api_pricing("gpt-6.1-sol", tier, usage)
    assert short["cost_usd"] == pytest.approx((62000 * 2 + 200000 * 0.1 + 10000 * 2.5 + 1000 * 10) / 1e6 * multiplier)
    long = api_pricing("gpt-6.1-sol", tier, {**usage, "prompt_tokens": 272001})
    assert long["cost_usd"] == pytest.approx((62001 * 4 + 200000 * 0.2 + 10000 * 5 + 1000 * 15) / 1e6 * multiplier)
    assert long["billing_model"] != short["billing_model"]


def test_unknown_model_and_unknown_usage_are_not_assigned_prices():
    from tracing.codex.hooks.pricing import api_pricing

    assert api_pricing("synthetic-model", "priority", {"prompt_tokens": 10}) is None
    assert api_pricing("gpt-6.1-sol", "priority", {}) is None
    assert api_pricing("gpt-6.1-sol", "unknown-tier", {"prompt_tokens": 10, "completion_tokens": 2}) is None


def test_service_tier_change_is_scoped_to_subsequent_responses(tmp_path):
    rows = modern_records()
    rows.insert(
        1, record("event_msg", {"type": "thread_settings_applied", "thread_settings": {"service_tier": "priority"}})
    )
    rows.insert(3, record("turn_context", {"model": "gpt-6.1-sol", "turn_id": "t"}))
    index = next(i for i, row in enumerate(rows) if row["payload"].get("role") == "assistant")
    rows.insert(
        index, record("event_msg", {"type": "thread_settings_applied", "thread_settings": {"service_tier": "flex"}})
    )
    turn = _extract_turn_from_rollout(write(tmp_path, rows), "t")
    llms = [attrs(s) for s in spans(turn) if attrs(s)["openinference.span.kind"] == "LLM"]
    assert [a["llm.model_name"] for a in llms] == ["gpt-6.1-sol [priority]", "gpt-6.1-sol [flex]"]


def test_unknown_tier_cannot_match_phoenix_standard_prices(tmp_path):
    from tracing.codex.hooks.pricing import phoenix_model_inputs

    rows = modern_records()
    rows.insert(
        1, record("event_msg", {"type": "thread_settings_applied", "thread_settings": {"service_tier": "future-tier"}})
    )
    rows.insert(3, record("turn_context", {"model": "gpt-6.1-sol", "turn_id": "t"}))
    turn = _extract_turn_from_rollout(write(tmp_path, rows), "t")
    for span in spans(turn):
        a = attrs(span)
        if a["openinference.span.kind"] != "LLM":
            continue
        assert "api_cost.estimated_usd" not in a
        assert a["codex.model_name"] == "gpt-6.1-sol"
        patterns = [p["namePattern"] for p in phoenix_model_inputs()] + [r"gpt-6\.1-sol"]
        assert not any(re.search(pattern, a["llm.model_name"]) for pattern in patterns)


def test_partial_usage_cannot_match_phoenix_standard_prices():
    from tracing.codex.hooks.pricing import phoenix_model_inputs

    turn = {"model": "gpt-6.1-sol", "llm_calls": [{"token_usage": {"prompt_tokens": 100}}]}
    llm = next(attrs(s) for s in spans(turn) if attrs(s)["openinference.span.kind"] == "LLM")
    assert llm["api_cost.basis"] == "unpriced_usage"
    assert "api_cost.estimated_usd" not in llm
    patterns = [p["namePattern"] for p in phoenix_model_inputs()] + [r"gpt-6\.1-sol"]
    assert not any(re.search(pattern, llm["llm.model_name"]) for pattern in patterns)
