"""Published API equivalents; subscription charges are not inferred."""

from __future__ import annotations

import re

RATE_DATE = "2026-10-21"
RATE_SOURCE = "https://developers.openai.com/api/docs/models/gpt-6.1-sol"
_MULTIPLIERS = {"standard": 1, "priority": 2, "flex": 0.5, "ultrafast": 6}


def api_pricing(model: str, service_tier: str, usage: dict) -> dict | None:
    if model != "gpt-6.1-sol":
        return None
    prompt, output = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if any(type(v) is not int or v < 0 for v in (prompt, output)):
        return None
    tier = {"": "standard", "default": "standard", "fast": "priority"}.get(service_tier, service_tier)
    if tier not in _MULTIPLIERS:
        return None
    long_context = prompt > 272000
    # Input already includes cache reads/writes; output already includes reasoning.
    cached = min(prompt, _count(usage.get("cached_input_tokens")))
    written = min(prompt - cached, _count(usage.get("cache_write_input_tokens")))
    counts = {"input": prompt - cached - written, "cache_read": cached, "cache_write": written, "output": output}
    rates = dict(
        zip(("input", "cache_read", "cache_write", "output"), (4, 0.2, 5, 15) if long_context else (2, 0.1, 2.5, 10))
    )
    rates = {key: value * _MULTIPLIERS[tier] for key, value in rates.items()}
    labels = ([] if tier == "standard" else [tier]) + (["long context"] if long_context else [])
    billing_model = model + (" [" + ", ".join(labels) + "]" if labels else "")
    return {
        "billing_model": billing_model,
        "tier": tier,
        "rates_per_million": rates,
        "counts": counts,
        "cost_usd": sum(counts[key] * rate for key, rate in rates.items()) / 1_000_000,
    }


def _count(value: object) -> int:
    return value if type(value) is int and value >= 0 else 0


def phoenix_model_inputs() -> list[dict]:
    """Inputs for Phoenix's createModel mutation; no network or credential access."""
    result = []
    for tier in _MULTIPLIERS:
        for prompt in (1, 272001):
            price = api_pricing("gpt-6.1-sol", tier, {"prompt_tokens": prompt, "completion_tokens": 0})
            assert price is not None
            name = price["billing_model"]
            result.append(
                {
                    "name": name,
                    "provider": "openai",
                    "namePattern": "^" + re.escape(name) + "$",
                    "costs": [
                        {
                            "tokenType": key,
                            "kind": "COMPLETION" if key == "output" else "PROMPT",
                            "costPerMillionTokens": rate,
                        }
                        for key, rate in price["rates_per_million"].items()
                    ],
                }
            )
    return result
