"""Codex default location for shared hash-only delivery receipts."""

from __future__ import annotations

from pathlib import Path

from core.constants import STATE_BASE_DIR
from core.delivery import deliver as _deliver

_STATE_DIR = STATE_BASE_DIR / "codex" / "delivery"


def deliver(payload: dict, send, state_dir: Path | None = None) -> bool:
    return _deliver(payload, send, _STATE_DIR if state_dir is None else state_dir)
