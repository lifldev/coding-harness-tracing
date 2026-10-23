"""Synthetic exporter contract checks; no private sessions or backend calls."""

import copy
import json
import subprocess
from types import SimpleNamespace

import pytest

from core import common
from core import work_context as wc


@pytest.fixture(autouse=True)
def isolate_work_context(monkeypatch):
    for key in (
        "HARNESS_TICKET",
        "AUTOLOOP_TICKET_ID",
        "HARNESS_PR",
        "HARNESS_RUN_ID",
        "HARNESS_TRACE_CONTEXT",
        "HARNESS_AGENT_ROLE",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(wc, "_SESSION_CWDS", {})
    monkeypatch.setattr(wc.socket, "gethostname", lambda: "test-host.example.org")
    wc.git_context.cache_clear()
    yield
    wc.git_context.cache_clear()


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/ExampleClient/app.git",
        "https://github.com/ExampleClient/app",
        "ssh://git@github.com/ExampleClient/app.git",
        "ssh://git@github.com:2222/ExampleClient/app.git",
        "git@github.com:ExampleClient/app.git",
        "github.com:ExampleClient/app",
        "git://github.com/ExampleClient/app.git",
    ],
)
def test_remote_forms(remote):
    assert wc.parse_remote(remote) == "ExampleClient/app"


@pytest.mark.parametrize(
    "remote", ["", "/local/repo.git", "../repo", "file:///local/repo.git", "https://github.com/owner", "ssh://[broken"]
)
def test_unknown_remotes(remote):
    assert wc.parse_remote(remote) == ""


@pytest.fixture
def config(monkeypatch):
    cfg = {
        "project_routes": [{"owner": "exampleclient", "project": "client"}, {"owner": "ExampleClient", "project": "later"}],
        "default_project": "personal",
        "harnesses": {
            h: {"target": "phoenix", "endpoint": "http://fixture", "project_name": h} for h in ("claude-code", "codex")
        },
    }
    monkeypatch.setattr("core.config.load_config", lambda: cfg)
    return cfg


def payload(harness="codex", cwd="/synthetic/repo"):
    attrs = {"session.id": "fixture-session"}
    if cwd:
        attrs["codex.cwd"] = cwd
    return common.build_span("Turn", "CHAIN", "1" * 16, "2" * 32, attrs=attrs, service_name=harness)


def attrs_of(span):
    return {a["key"]: a["value"].get("stringValue") for a in span["attributes"]}


def spans_of(data):
    return [s for rs in data["resourceSpans"] for ss in rs["scopeSpans"] for s in ss["spans"]]


@pytest.fixture
def git(monkeypatch):
    calls = []

    def run(command, **kwargs):
        assert command[:3] == ["git", "-C", "/synthetic/repo"]
        assert kwargs == {"capture_output": True, "text": True, "timeout": 1, "check": True}
        calls.append(command)
        return SimpleNamespace(
            stdout="git@github.com:ExampleClient/app.git\n" if command[3] == "remote" else "feature/ab-894-next/AB-12\n"
        )

    monkeypatch.setattr(wc.subprocess, "run", run)
    return calls


def test_routing_precedence(config, git, monkeypatch):
    data = payload()
    assert common.resolve_backend(data)["project_name"] == "client"
    assert common.resolve_backend(data)["project_name"] == "client"
    assert len(git) == 2
    monkeypatch.setenv("PHOENIX_PROJECT_NAME", "sandbox")
    assert common.resolve_backend(data)["project_name"] == "sandbox"
    monkeypatch.delenv("PHOENIX_PROJECT_NAME")
    config["project_routes"] = []
    assert common.resolve_backend(data)["project_name"] == "personal"
    config.pop("default_project")
    assert common.resolve_backend(data)["project_name"] == "codex"
    monkeypatch.setenv("ARIZE_API_KEY", "synthetic")
    monkeypatch.setenv("ARIZE_SPACE_ID", "synthetic")
    monkeypatch.setenv("ARIZE_PROJECT_NAME", "sandbox")
    assert common.resolve_backend(data)["project_name"] == "sandbox"


@pytest.mark.parametrize(
    "failure",
    [
        OSError("no git"),
        subprocess.TimeoutExpired("git", 1),
        subprocess.CalledProcessError(1, "git"),
        ValueError("embedded null byte"),
    ],
)
def test_git_failure_still_exports(config, monkeypatch, failure):
    calls = []

    def fail(*args, **kwargs):
        calls.append(args)
        raise failure

    monkeypatch.setattr(wc.subprocess, "run", fail)
    sent = []
    monkeypatch.setattr(common, "otlp_json_to_protobuf", lambda p: sent.append(p) or b"fixture")
    monkeypatch.setattr(common, "_post_otlp", lambda *args: True)
    assert common.send_span(payload())
    assert common.send_span(payload())
    assert len(calls) == 2
    attrs = attrs_of(spans_of(sent[0])[0])
    assert attrs["work.client"] == "personal"
    assert "work.repo" not in attrs and "work.branch" not in attrs


@pytest.mark.parametrize(
    "environment, expected",
    [
        ({}, "AB-894"),
        ({"AUTOLOOP_TICKET_ID": "AB-2"}, "AB-2"),
        ({"HARNESS_TICKET": "CD-3", "AUTOLOOP_TICKET_ID": "AB-2"}, "CD-3"),
    ],
)
def test_ticket_precedence(monkeypatch, environment, expected):
    for k, v in environment.items():
        monkeypatch.setenv(k, v)
    assert wc.work_attributes("codex", "personal", {"work.branch": "feature/ab-894/ZZ-9"})["work.ticket"] == expected


@pytest.mark.parametrize(
    "environment, expected",
    [
        ({}, "interactive"),
        ({"HARNESS_AGENT_ROLE": "invalid"}, "interactive"),
        ({"AUTOLOOP_TICKET_ID": "AB-1"}, "worker"),
        ({"HARNESS_TRACE_CONTEXT": "malformed"}, "worker"),
        *[
            ({"HARNESS_AGENT_ROLE": r, "AUTOLOOP_TICKET_ID": "AB-1"}, r)
            for r in ("coordinator", "worker", "reviewer", "qa", "interactive")
        ],
    ],
)
def test_role_defaults(monkeypatch, environment, expected):
    for k, v in environment.items():
        monkeypatch.setenv(k, v)
    assert wc.work_attributes("codex", "personal", {})["agent.role"] == expected


@pytest.mark.parametrize(
    "context, expected", [(' {"runId":"run-1"}', "run-1"), ('{"runId":42}', None), ("[]", None), ("broken", None)]
)
def test_run_id(monkeypatch, context, expected):
    monkeypatch.setenv("HARNESS_TRACE_CONTEXT", context)
    assert wc.work_attributes("codex", "personal", {}).get("work.run_id") == expected
    monkeypatch.setenv("HARNESS_RUN_ID", "explicit")
    assert wc.work_attributes("codex", "personal", {})["work.run_id"] == "explicit"


def test_metadata_merge_all_spans(config, git, monkeypatch):
    monkeypatch.setenv("HARNESS_PR", "https://github.com/ExampleClient/app/pull/1")
    data = payload()
    spans_of(data)[0]["attributes"].append(
        {"key": "metadata", "value": {"stringValue": '{"nested":{"value":1},"work.ticket":"old"}'}}
    )
    # A lineage AGENT before the root has no cwd, but must receive its context.
    agent = common.build_span(
        "Agent", "AGENT", "3" * 16, "2" * 32, attrs={"session.id": "fixture-session"}, service_name="codex"
    )
    data["resourceSpans"][0]["scopeSpans"][0]["spans"].insert(0, spans_of(agent)[0])
    original = copy.deepcopy(data)
    enriched = wc.enrich_payload(data, "client")
    assert data == original
    for span in spans_of(enriched):
        attrs = attrs_of(span)
        assert attrs["work.client"] == "client"
        assert attrs["work.repo"] == "ExampleClient/app"
        assert attrs["work.ticket"] == "AB-894"
        assert attrs["agent.host"] == "test-host"
        assert attrs["gen_ai.provider.name"] == "openai"
        metadata = json.loads(attrs["metadata"])
        assert all(metadata[k] == v for k, v in attrs.items() if k.startswith(("work.", "agent.")))
        assert not any(k.startswith("gen_ai.") for k in metadata)
    assert json.loads(attrs_of(spans_of(enriched)[1])["metadata"])["nested"] == {"value": 1}


def test_claude_hook_cwd_survives_later_missing_payload(config, git, monkeypatch, tmp_path):
    from tracing.claude_code.hooks import adapter, handlers

    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path)
    sent = []
    monkeypatch.setattr(common, "otlp_json_to_protobuf", lambda p: sent.append(p) or b"fixture")
    monkeypatch.setattr(common, "_post_otlp", lambda *args: True)
    handlers._handle_session_start({"session_id": "fixture-session", "cwd": "/synthetic/repo"})
    handlers._handle_user_prompt_submit({"session_id": "fixture-session", "prompt": "synthetic"})
    wc._SESSION_CWDS.clear()  # Simulate the next hook process reading persisted cwd.
    handlers._handle_notification({"session_id": "fixture-session", "message": "synthetic"})
    assert sent
    for span in spans_of(sent[0]):
        attrs = attrs_of(span)
        assert attrs["agent.harness"] == attrs["gen_ai.agent.name"] == "claude-code"
        assert attrs["gen_ai.provider.name"] == "anthropic"
        assert attrs["work.repo"] == "ExampleClient/app"
        assert attrs["work.client"] == "client"


def test_codex_rendered_and_fallback_spans(config, git, monkeypatch, tmp_path):
    from tracing.codex.hooks import delivery, handlers

    monkeypatch.setattr(delivery, "_STATE_DIR", tmp_path)
    sent = []
    monkeypatch.setattr(common, "otlp_json_to_protobuf", lambda p: sent.append(p) or b"fixture")
    monkeypatch.setattr(common, "_post_otlp", lambda *args: True)
    handlers._build_and_send_spans(
        "fixture-session",
        "turn-1",
        {"cwd": "/synthetic/repo", "user_prompt": "synthetic", "assistant_text": "synthetic"},
    )
    handlers._send_legacy_single_span("fixture-session", "turn-2", {"cwd": "/synthetic/repo"})
    assert len(sent) == 2
    for data in sent:
        for span in spans_of(data):
            attrs = attrs_of(span)
            assert attrs["agent.harness"] == attrs["gen_ai.agent.name"] == "codex"
            assert attrs["gen_ai.provider.name"] == "openai"
            assert attrs["work.repo"] == "ExampleClient/app"
            assert attrs["work.client"] == "client"


def test_unknown_values_omitted_and_other_harness_project_unchanged(monkeypatch):
    monkeypatch.setattr(wc.subprocess, "run", lambda *a, **kw: pytest.fail("unknown cwd must not run git"))
    attrs = wc.work_attributes("codex", "", {})
    assert not any(
        k in attrs for k in ("work.client", "work.repo", "work.branch", "work.ticket", "work.pr", "work.run_id")
    )
    assert all(isinstance(v, str) for v in attrs.values())
    assert common.resolve_backend(payload("other", cwd=""))["project_name"] == "other"
