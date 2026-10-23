"""Fail-soft session work context shared by the hook exporters."""

import functools
import json
import os
import re
import socket
import subprocess
from urllib.parse import urlsplit

_SESSION_CWDS: dict = {}


def register_session_cwd(harness: str, session_id: str, cwd: str) -> None:
    if isinstance(cwd, str) and cwd:
        _SESSION_CWDS.setdefault((harness, session_id), cwd)


def parse_remote(remote: str) -> str:
    """Normalize network origin URLs to owner/name; ignore local remotes."""
    try:
        if "://" in remote:
            parsed = urlsplit(remote)
            if parsed.scheme not in ("http", "https", "ssh", "git") or not parsed.hostname:
                return ""
            path = parsed.path
        else:
            match = re.fullmatch(r"(?:[^/@:]+@)?[^/:]+:(.+)", remote)
            if not match:
                return ""
            path = match.group(1)
        path = path.strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        parts = path.split("/")
        return path if len(parts) == 2 and all(parts) else ""
    except ValueError:
        return ""


@functools.lru_cache(maxsize=1024)
def git_context(harness: str, session_id: str, cwd: str) -> dict:
    """Snapshot git facts once per session, including unsuccessful lookups."""
    if not cwd:
        return {}

    def git(*args: str) -> str:
        try:
            result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=1, check=True)
            return result.stdout.strip()
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
            return ""

    repo = parse_remote(git("remote", "get-url", "origin"))
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD")
    return {key: value for key, value in (("work.repo", repo), ("work.branch", branch)) if value}


def session_context(payload: dict) -> tuple:
    harness = ""
    session_id = ""
    cwd = ""
    for resource in payload.get("resourceSpans", []):
        for attr in resource.get("resource", {}).get("attributes", []):
            if attr.get("key") == "service.name":
                harness = attr.get("value", {}).get("stringValue", "")
        for scope in resource.get("scopeSpans", []):
            for span in scope.get("spans", []):
                attrs = {a["key"]: a.get("value", {}).get("stringValue", "") for a in span.get("attributes", [])}
                session_id = session_id or attrs.get("session.id", "")
                cwd = cwd or attrs.get("codex.cwd", "")
    cwd = _SESSION_CWDS.get((harness, session_id), cwd)
    return harness, session_id, cwd


def work_attributes(harness: str, project: str, facts: dict) -> dict:
    attrs = {**facts, "work.client": project, "agent.harness": harness}
    trace_context = os.environ.get("HARNESS_TRACE_CONTEXT", "")
    try:
        context = json.loads(trace_context) if trace_context else {}
        run_id = context.get("runId", "") if isinstance(context, dict) else ""
    except (ValueError, TypeError):
        run_id = ""
    ticket = os.environ.get("HARNESS_TICKET") or os.environ.get("AUTOLOOP_TICKET_ID")
    if not ticket:
        match = re.search(r"[A-Za-z]{2,10}-\d+", facts.get("work.branch", ""))
        ticket = match.group().upper() if match else ""
    role = os.environ.get("HARNESS_AGENT_ROLE", "")
    if role not in {"coordinator", "worker", "reviewer", "qa", "interactive"}:
        role = "worker" if os.environ.get("AUTOLOOP_TICKET_ID") or trace_context else "interactive"
    try:
        host = socket.gethostname().split(".")[0]
    except OSError:
        host = ""
    attrs.update(
        {
            "work.ticket": ticket,
            "work.pr": os.environ.get("HARNESS_PR", ""),
            "work.run_id": os.environ.get("HARNESS_RUN_ID") or run_id,
            "agent.role": role,
            "agent.host": host,
            "gen_ai.agent.name": harness,
            "gen_ai.provider.name": {"claude-code": "anthropic", "codex": "openai"}.get(harness, ""),
        }
    )
    return {key: value for key, value in attrs.items() if isinstance(value, str) and value}


def enrich_payload(payload: dict, project: str) -> dict:
    harness, session_id, cwd = session_context(payload)
    context = work_attributes(harness, project, git_context(harness, session_id, cwd))
    metadata_context = {k: v for k, v in context.items() if not k.startswith("gen_ai.")}

    def enrich(span: dict) -> dict:
        attrs = list(span.get("attributes", []))
        metadata = {}
        for attr in attrs:
            if attr.get("key") == "metadata":
                try:
                    value = json.loads(attr.get("value", {}).get("stringValue", ""))
                    if isinstance(value, dict):
                        metadata.update(value)
                except (ValueError, TypeError):
                    pass
        metadata.update(metadata_context)
        values = {**context, "metadata": json.dumps(metadata)}
        attrs = [a for a in attrs if a.get("key") not in values]
        attrs.extend({"key": k, "value": {"stringValue": v}} for k, v in values.items())
        return {**span, "attributes": attrs}

    return {
        **payload,
        "resourceSpans": [
            {
                **rs,
                "scopeSpans": [
                    {**ss, "spans": [enrich(span) for span in ss.get("spans", [])]} for ss in rs.get("scopeSpans", [])
                ],
            }
            for rs in payload.get("resourceSpans", [])
        ],
    }
