"""Native CDP runner input safety and isolation contracts."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from typing import cast

import pytest
from examples.live.browser_e2e import cdp_runner

if TYPE_CHECKING:
    from pathlib import Path

    from websockets.sync.client import ClientConnection


@pytest.mark.parametrize(
    "value",
    [
        "http://example.test",
        "http://127.0.0.1:9222/devtools",
        "http://127.0.0.1:9222/?token=x",
        "http://user:secret@127.0.0.1:9222",
    ],
)
def test_cdp_endpoint_must_be_plain_loopback_root(value: str) -> None:
    with pytest.raises(ValueError):
        cdp_runner.loopback_url(value, allowed_schemes={"http", "https"})


def test_cdp_endpoint_accepts_local_browser_hosts() -> None:
    assert (
        cdp_runner.loopback_url("http://127.0.0.1:9222", allowed_schemes={"http", "https"}) == "http://127.0.0.1:9222"
    )


def test_browser_websocket_validation_uses_exact_localhost_and_browser_endpoint() -> None:
    assert cdp_runner.loopback_websocket("ws://localhost:9222/devtools/browser/owned")
    with pytest.raises(ValueError):
        cdp_runner.loopback_websocket("ws://localhost.evil.test:9222/devtools/browser/owned")
    with pytest.raises(ValueError):
        cdp_runner.loopback_websocket("ws://localhost:9222/devtools/page/other")


def test_failure_message_redacts_credential_like_values() -> None:
    message = cdp_runner.sanitized_error(RuntimeError("api_key=sk-test-secret123 provider failed"))
    assert "sk-test-secret123" not in message
    assert "[redacted]" in message
    assert (
        cdp_runner.loopback_url("http://localhost:9222/", allowed_schemes={"http", "https"}) == "http://localhost:9222"
    )


def test_authenticated_scenario_must_require_real_work(tmp_path: Path) -> None:
    scenario: dict[str, object] = {"rounds": [{"text": "one"}, {"text": "two"}, {"text": "three"}]}
    with pytest.raises(ValueError, match="actual tool-call"):
        cdp_runner.prepare_scenario(tmp_path, scenario, "authenticated")


def test_authenticated_scenario_rejects_short_conversation(tmp_path: Path) -> None:
    scenario: dict[str, object] = {"rounds": [{"text": "one", "expect_text": "one"}]}
    with pytest.raises(ValueError, match="at least three"):
        cdp_runner.prepare_scenario(tmp_path, scenario, "authenticated")


def test_cdp_calls_match_responses_by_id_and_session(monkeypatch: pytest.MonkeyPatch) -> None:
    class Socket:
        def __init__(self) -> None:
            self.request: dict[str, object] = {}
            self.responses = iter(
                [
                    json.dumps({"method": "Page.loadEventFired", "params": {}}),
                    json.dumps({"id": 1, "result": {"targetId": "owned"}}),
                ]
            )

        def send(self, value: str) -> None:
            self.request = cast("dict[str, object]", json.loads(value))

        def recv(self, timeout: int = 30) -> str:
            del timeout
            return next(self.responses)

    fake_socket = Socket()
    monkeypatch.setattr(cdp_runner, "connect", lambda *_args, **_kwargs: cast("ClientConnection", fake_socket))
    client = cdp_runner.CDP("ws://localhost/devtools/browser/test")
    result = client.call("Target.createTarget", {"url": "about:blank"}, session="browser-session")
    assert result == {"targetId": "owned"}
    assert fake_socket.request["sessionId"] == "browser-session"


def test_cdp_evaluate_returns_value_and_surfaces_runtime_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    class Socket:
        def __init__(self, response: dict[str, object]) -> None:
            self.response = response

        def send(self, _value: str) -> None:
            pass

        def recv(self, timeout: int = 30) -> str:
            del timeout
            return json.dumps({"id": 1, "result": self.response})

    socket = Socket({"result": {"type": "string", "value": "ready"}})
    monkeypatch.setattr(cdp_runner, "connect", lambda *_args, **_kwargs: cast("ClientConnection", socket))
    client = cdp_runner.CDP("ws://localhost/devtools/browser/test")
    assert client.evaluate("document.readyState", session="owned") == "ready"

    failure = Socket({"exceptionDetails": {"text": "SyntaxError"}, "result": {"type": "undefined"}})
    monkeypatch.setattr(cdp_runner, "connect", lambda *_args, **_kwargs: cast("ClientConnection", failure))
    client = cdp_runner.CDP("ws://localhost/devtools/browser/test")
    with pytest.raises(RuntimeError, match="Browser evaluation failed"):
        client.evaluate("(()", session="owned")


def test_realistic_continuous_live_queue_and_late_run_id_allow_next_round(monkeypatch: pytest.MonkeyPatch) -> None:
    """A live RTP silence queue need not drain; a queued handoff has no run ID yet."""
    state = {
        "transcripts": [
            {"speaker": "user", "text": "Read the fixture", "monotonicMs": 1100},
            {"speaker": "assistant", "text": "Audio ready", "monotonicMs": 1500},
        ],
        "delegations": [
            {
                "delegationId": "handoff",
                "eventType": "delegation",
                "status": status,
                "runId": "" if status == "queued" else "run",
                "monotonicMs": 1200,
            }
            for status in ("queued", "working", "completed")
        ],
        "toolCalls": [
            {"callId": "call", "runId": "run", "eventType": kind, "monotonicMs": 1300}
            for kind in ("tool_call", "tool_execution_started", "tool_result")
        ],
        "lastAudibleOutputMs": 2000,
        "health": {"receivedSamples": 120000, "playedSamples": 115200, "bufferedMs": 200, "targetMs": 200},
    }
    clock = iter((1000, 6000))

    class Browser:
        def evaluate(self, expression: str, **_kwargs: object) -> object:
            if expression == "performance.now()":
                return next(clock)
            if expression == "globalThis.__voiceE2E.status()":
                return state
            if expression == "globalThis.__voiceE2E.finishMeasurement()":
                return {"verified": True}
            return None

    clicks: list[list[str]] = []
    monkeypatch.setattr(cdp_runner, "click_button", lambda _cdp, _session, labels, **_kwargs: clicks.append(labels))
    monkeypatch.setattr(cdp_runner, "wait_for", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cdp_runner, "export_capture", lambda *_args: {"captured": True})
    monkeypatch.setattr(time, "sleep", lambda _seconds: pytest.fail("Healthy silence queue blocked progress"))
    rounds: list[dict[str, object]] = [
        {
            "id": "round",
            "expect_text": "audio ready",
            "require_delegation": True,
            "require_subagent": False,
            "require_tool_call": True,
        }
    ]
    assert cdp_runner.scenario_capture(cast("cdp_runner.CDP", Browser()), "owned", rounds, 60) == {"captured": True}
    assert clicks == [["Start voice"], ["End voice"]]


def test_export_bound_clears_frozen_browser_pcm_even_on_failure() -> None:
    expressions: list[str] = []

    class Browser:
        def evaluate(self, expression: str, **_kwargs: object) -> object:
            expressions.append(expression)
            return 256_000_001 if expression.endswith(".length") else None

    with pytest.raises(RuntimeError, match="export bound"):
        cdp_runner.export_capture(cast("cdp_runner.CDP", Browser()), "owned")
    assert sum("JSON.stringify" in expression for expression in expressions) == 1
    assert expressions[-1] == "delete globalThis.__voiceE2EExportJson"
