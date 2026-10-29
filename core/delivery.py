"""Successful-delivery receipts contain hashes only, never transcript content."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from core.common import FileLock, env


def deliver(payload: dict, send, state_dir: Path) -> bool:
    spans = payload["resourceSpans"][0]["scopeSpans"][0]["spans"]
    if not spans:
        return False
    if env.dry_run:
        return send(payload)
    root = Path(state_dir)
    key = hashlib.sha256(spans[0]["traceId"].encode()).hexdigest()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    receipt = root / f"{key}.json"
    # A timed-out lock must never remove another live notify's lock inode.
    with FileLock(root / f"{key}.lock", break_on_timeout=False):
        try:
            previous = json.loads(receipt.read_text())
            if not isinstance(previous, dict):
                previous = {}
        except (OSError, ValueError):
            previous = {}
        hashes = {s["spanId"]: hashlib.sha256(json.dumps(s, sort_keys=True).encode()).hexdigest() for s in spans}
        changed = [s for s in spans if previous.get(s["spanId"]) != hashes[s["spanId"]]]
        if not changed:
            return True
        # Avoid mutating the caller's payload, so retry uses identical IDs/content.
        envelope = dict(payload)
        resource = dict(payload["resourceSpans"][0])
        scope = dict(resource["scopeSpans"][0])
        scope["spans"] = changed
        resource["scopeSpans"] = [scope]
        envelope["resourceSpans"] = [resource]
        if not send(envelope):
            return False
        previous.update(hashes)
        temp = receipt.with_suffix(f".{os.getpid()}.tmp")
        try:
            temp.write_text(json.dumps(previous, sort_keys=True))
            temp.chmod(0o600)
            temp.replace(receipt)
        finally:
            temp.unlink(missing_ok=True)
    return True
