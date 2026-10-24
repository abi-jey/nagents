"""Run voice E2E scenarios in an owned, isolated Chrome context over native CDP."""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

from examples.live.browser_e2e.artifacts import check_authenticated_scenario
from examples.live.browser_e2e.artifacts import check_capture
from examples.live.browser_e2e.artifacts import expected_phrases
from examples.live.browser_e2e.artifacts import normalized_text
from examples.live.browser_e2e.artifacts import speech
from examples.live.browser_e2e.artifacts import validate_wav
from websockets.sync.client import connect

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
MAX_ROUNDS = 64
SENTINEL = "NGN-VOICE-E2E-5017"


class CDP:
    def __init__(self, websocket_url: str) -> None:
        self.socket = connect(websocket_url, open_timeout=5, close_timeout=2, max_size=64 * 1024 * 1024)
        self.next_id = 0

    def call(self, method: str, params: dict[str, object] | None = None, *, session: str = "") -> dict[str, object]:
        self.next_id += 1
        identifier = self.next_id
        payload: dict[str, object] = {"id": identifier, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        self.socket.send(json.dumps(payload))
        while True:
            message = json.loads(self.socket.recv(timeout=30))
            if message.get("id") != identifier:
                continue
            if "error" in message:
                raise RuntimeError(f"CDP {method} failed: {message['error']}")
            return cast("dict[str, object]", message.get("result", {}))

    def evaluate(self, expression: str, *, session: str, await_promise: bool = True) -> object:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": await_promise, "returnByValue": True},
            session=session,
        )
        details = result
        if "exceptionDetails" in result:
            raise RuntimeError(f"Browser evaluation failed: {result['exceptionDetails']}")
        remote = cast("dict[str, object]", details.get("result", {}))
        if "unserializableValue" in remote:
            raise RuntimeError(f"Browser returned unserializable value: {remote['unserializableValue']}")
        return remote.get("value")


def loopback_url(value: str, *, allowed_schemes: set[str]) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in allowed_schemes or parsed.hostname not in LOOPBACK:
        raise ValueError("CDP and app URLs must use a loopback host and an allowed local scheme")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("CDP and app URLs cannot include credentials, query strings, or fragments")
    if parsed.path not in ("", "/"):
        raise ValueError("CDP endpoint URLs must not include a path")
    return value.rstrip("/")


def loopback_websocket(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"ws", "wss"} or parsed.hostname not in LOOPBACK:
        raise ValueError("Chrome must expose CDP on an exact loopback websocket host")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or not parsed.port:
        raise ValueError("Chrome CDP websocket must have a valid port and no credentials, query, or fragment")
    if not parsed.path.startswith("/devtools/browser/"):
        raise ValueError("Chrome CDP websocket must be a browser-level DevTools endpoint")
    return value


def read_json(url: str) -> dict[str, object]:
    with urllib.request.urlopen(url, timeout=3) as response:
        return cast("dict[str, object]", json.loads(response.read()))


def numeric(value: object, default: float = 0) -> float:
    return float(cast("int | float", value)) if type(value) in {int, float} else default


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as source:
        return source.getnframes() / source.getframerate()


def sanitized_error(error: BaseException) -> str:
    value = str(error)[:1000]
    value = re.sub(r"(?i)(authorization|token|api[_-]?key|password)(\s*[:=]\s*)[^\s,;]+", r"\1\2[redacted]", value)
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[redacted]", value)


def wait_http(url: str, process: subprocess.Popen[str] | None, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError("Local E2E server exited before becoming ready")
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.2)
    raise TimeoutError(f"Local app did not become ready at {url}")


def verify_capture_routes(origin: str) -> None:
    for path in (
        "/assets/voice-e2e-capture.js",
        "/assets/voice-e2e-input-round-1.wav",
        "/__voice-e2e/meta",
    ):
        try:
            with urllib.request.urlopen(f"{origin}{path}", timeout=3) as response:
                if response.status != 200 or not response.read(16):
                    raise ValueError("empty response")
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            raise RuntimeError("The local app is missing the private browser E2E capture routes") from error


def accessible_button(cdp: CDP, session: str, labels: list[str]) -> dict[str, float] | None:
    expression = f"""(() => {{
      const wanted = {json.dumps(labels)};
      const buttons = [...document.querySelectorAll('button')];
      const button = buttons.find(item => {{
        const label = (item.getAttribute('aria-label') || item.innerText || '').trim();
        return !item.closest('[inert],[aria-hidden="true"]') && wanted.some(value => label === value || label.startsWith(value));
      }});
      if (!button || button.disabled) return null;
      const rect = button.getBoundingClientRect();
      if (!rect.width || !rect.height || getComputedStyle(button).visibility === 'hidden') return null;
      const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
      const hit = document.elementFromPoint(x, y);
      if (!hit || (hit !== button && !button.contains(hit))) return null;
      return {{x, y}};
    }})()"""
    value = cdp.evaluate(expression, session=session)
    return cast("dict[str, float] | None", value)


def click_button(cdp: CDP, session: str, labels: list[str], timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    point: dict[str, float] | None = None
    while time.monotonic() < deadline:
        point = accessible_button(cdp, session, labels)
        if point:
            break
        time.sleep(0.2)
    if not point:
        raise TimeoutError(f"No enabled visible button matched accessible label {labels!r}")
    for event, button in (("mouseMoved", "none"), ("mousePressed", "left"), ("mouseReleased", "left")):
        cdp.call(
            "Input.dispatchMouseEvent",
            {"type": event, "x": point["x"], "y": point["y"], "button": button, "clickCount": 1},
            session=session,
        )


def wait_for(cdp: CDP, session: str, expression: str, timeout: float, description: str) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = cdp.evaluate(expression, session=session)
        if result:
            return result
        time.sleep(0.25)
    raise TimeoutError(f"Timed out waiting for {description}")


def safe_json_file(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def prepare_scenario(fixtures: Path, scenario: dict[str, object], mode: str) -> tuple[Path, list[dict[str, object]]]:
    rounds = scenario.get("rounds")
    if not isinstance(rounds, list) or not rounds or len(rounds) > MAX_ROUNDS:
        raise ValueError("Scenario must contain 1 to 64 rounds")
    if mode == "authenticated":
        if len(rounds) < 3:
            raise ValueError("Authenticated scenarios need at least three separate conversational rounds")
        if not any(isinstance(item, dict) and item.get("require_tool_call") is True for item in rounds):
            raise ValueError("Authenticated scenarios must require actual tool-call progress")
        if not any(isinstance(item, dict) and item.get("require_delegation") is True for item in rounds):
            raise ValueError("Authenticated scenarios must require a completed delegation lifecycle")
        if any(not isinstance(item, dict) or not expected_phrases(item.get("expect_text")) for item in rounds):
            raise ValueError("Every authenticated round must require expected spoken response text")
    elif len(rounds) != 1:
        raise ValueError("Offline scripted mode supports exactly one fixture round")
    normalized: list[dict[str, object]] = []
    for index, raw in enumerate(rounds, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Round {index} must be an object")
        item = cast("dict[str, object]", raw)
        text = item.get("text")
        wav = item.get("wav")
        if not isinstance(text, str) and not isinstance(wav, str):
            raise ValueError(f"Round {index} needs local spoken text or a local WAV path")
        if isinstance(text, str) and not text.strip():
            raise ValueError(f"Round {index} spoken text cannot be empty")
        target = fixtures / f"round-{index}.wav"
        if isinstance(wav, str):
            shutil.copyfile(Path(wav).expanduser().resolve(), target)
        else:
            speech(target, cast("str", text))
        validate_wav(target)
        normalized.append(
            {
                "id": str(item.get("id", f"round-{index}"))[:100],
                "name": str(item.get("name", f"round-{index}"))[:100],
                "text": str(text or ""),
                "expect_text": item.get("expect_text", ""),
                "load": str(item.get("load", "baseline"))[:80],
                "require_tool_call": item.get("require_tool_call") is True,
                "require_delegation": item.get("require_delegation") is True,
                "require_subagent": item.get("require_subagent") is True,
                "wait_after_seconds": numeric(item.get("wait_after_seconds")),
                "wavPath": f"/assets/voice-e2e-input-round-{index}.wav",
                "fixtureSha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    if mode == "authenticated":
        if len(normalized) < 3:
            raise ValueError("Authenticated scenarios need at least three separate conversational rounds")
        if not any(item["require_tool_call"] for item in normalized):
            raise ValueError("Authenticated scenarios must require actual tool-call progress")
        if not any(item["require_delegation"] for item in normalized):
            raise ValueError("Authenticated scenarios must require a completed delegation lifecycle")
        if any(not item["expect_text"] for item in normalized):
            raise ValueError("Every authenticated round must require expected spoken response text")
        if any(
            numeric(item["wait_after_seconds"]) < 0 or numeric(item["wait_after_seconds"]) > 900 for item in normalized
        ):
            raise ValueError("wait_after_seconds must be between 0 and 900")
    return fixtures, normalized


def init_source(rounds: list[dict[str, object]], mode: str, max_seconds: int) -> str:
    config: dict[str, object] = {
        "mode": mode,
        "rounds": rounds,
        "maxSeconds": max_seconds,
    }
    source = Path(__file__).with_name("browser-init.js").read_text()
    return f"globalThis.__voiceE2EConfig = {json.dumps(config)};\n{source}"


def scenario_capture(
    cdp: CDP,
    session: str,
    rounds: list[dict[str, object]],
    timeout_seconds: int,
) -> dict[str, object]:
    click_button(cdp, session, ["Start voice"])
    try:
        wait_for(
            cdp,
            session,
            "globalThis.__voiceE2E?.status().microphone === true",
            8,
            "voice microphone initialization",
        )
    except TimeoutError:
        click_button(cdp, session, ["Show voice controls"])
        click_button(cdp, session, ["Start voice"])
        wait_for(
            cdp,
            session,
            "globalThis.__voiceE2E?.status().microphone === true",
            30,
            "voice microphone initialization",
        )
    wait_for(
        cdp,
        session,
        "globalThis.__voiceE2E?.status().ui.some(item => item.selector === '.voice-controls' && item.phase === 'connected')",
        45,
        "connected voice controls before transmitting the first fixture",
    )
    overall_deadline = time.monotonic() + timeout_seconds
    for index, item in enumerate(rounds, start=1):
        round_started_ms = numeric(cdp.evaluate("performance.now()", session=session))
        cdp.evaluate(f"globalThis.__voiceE2E.play(300, {index})", session=session)
        while time.monotonic() < overall_deadline:
            state = cast("dict[str, object]", cdp.evaluate("globalThis.__voiceE2E.status()", session=session))
            if state.get("stopped"):
                raise TimeoutError("Audio measurement stopped before the scenario completed")
            transcripts = [
                event
                for event in cast("list[dict[str, object]]", state.get("transcripts", []))
                if numeric(event.get("monotonicMs")) >= round_started_ms
            ]
            fixture_text = " ".join(
                str(event.get("text", "")) for event in transcripts if event.get("speaker") == "user"
            )
            assistant_text = " ".join(
                str(event.get("text", "")) for event in transcripts if event.get("speaker") == "assistant"
            )
            expected = expected_phrases(item.get("expect_text"))
            response = normalized_text(assistant_text)
            response_done = any(normalized_text(phrase) in response for phrase in expected)
            delegation_lifecycles: dict[str, set[str]] = {}
            for event in cast("list[dict[str, object]]", state.get("delegations", [])):
                if numeric(event.get("monotonicMs")) >= round_started_ms:
                    identifier = (
                        str(event.get("delegationId", ""))
                        if event.get("eventType") == "delegation"
                        else f"{event.get('runId', '')}:{event.get('sessionId', '')}"
                    )
                    if event.get("sessionId") or event.get("delegationId"):
                        status = str(event.get("status", ""))
                        event_type = str(event.get("eventType", ""))
                        lifecycle = status if event_type == "delegation" else event_type
                        if event_type == "task_completed" and status != "completed":
                            lifecycle = "task_failed"
                        delegation_lifecycles.setdefault(identifier, set()).add(lifecycle)
            delegation_done = not item["require_delegation"] or any(
                {"queued", "working", "completed"} <= lifecycle for lifecycle in delegation_lifecycles.values()
            )
            subagent_done = not item["require_subagent"] or any(
                {"task_started", "task_completed"} <= lifecycle and "task_failed" not in lifecycle
                for lifecycle in delegation_lifecycles.values()
            )
            no_pending_tasks = not any(
                "task_failed" in lifecycle for lifecycle in delegation_lifecycles.values()
            ) and all(
                "task_started" not in lifecycle or "task_completed" in lifecycle
                for lifecycle in delegation_lifecycles.values()
            )
            tool_done = True
            if item["require_tool_call"]:
                tool_lifecycles: dict[str, set[str]] = {}
                for tool_event in cast("list[dict[str, object]]", state.get("toolCalls", [])):
                    if numeric(tool_event.get("monotonicMs")) < round_started_ms:
                        continue
                    call_id = f"{tool_event.get('runId', '')}:{tool_event.get('callId', '')}"
                    if tool_event.get("callId"):
                        tool_lifecycles.setdefault(call_id, set()).add(str(tool_event.get("eventType", "")))
                tool_done = any(
                    {"tool_call", "tool_execution_started", "tool_result"} <= lifecycle
                    for lifecycle in tool_lifecycles.values()
                )
            health = cast("dict[str, object]", state.get("health", {}))
            queue_empty = numeric(health.get("receivedSamples")) <= numeric(health.get("playedSamples"))
            queue_age_ms = numeric(health.get("bufferedMs"), float("inf"))
            queue_bound_ms = min(320, max(100, numeric(health.get("targetMs")) + 40))
            last_audible = numeric(state.get("lastAudibleOutputMs"))
            if (
                fixture_text
                and response_done
                and delegation_done
                and subagent_done
                and no_pending_tasks
                and tool_done
                and last_audible > round_started_ms
            ):
                if queue_empty or queue_age_ms <= queue_bound_ms:
                    quiet_for_ms = numeric(cdp.evaluate("performance.now()", session=session)) - last_audible
                else:
                    quiet_for_ms = 0
                if quiet_for_ms >= 3000:
                    wait_after = numeric(item.get("wait_after_seconds"))
                    if wait_after:
                        if time.monotonic() + wait_after >= overall_deadline:
                            raise TimeoutError("Scenario waiting periods exceed the total measurement budget")
                        time.sleep(wait_after)
                    print(
                        json.dumps(
                            {
                                "event": "round.completed",
                                "round": index,
                                "id": item["id"],
                                "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            }
                        ),
                        flush=True,
                    )
                    break
            time.sleep(0.25)
        else:
            raise TimeoutError(f"Round {index} did not produce the expected transcript/delegation/audio")

    checkpoint = cdp.evaluate("globalThis.__voiceE2E.finishMeasurement()", session=session)
    if not isinstance(checkpoint, dict) or checkpoint.get("verified") is not True:
        raise RuntimeError("Audio measurement did not reach a complete source-to-render checkpoint")

    try:
        click_button(cdp, session, ["End voice"], timeout=1)
    except TimeoutError:
        click_button(cdp, session, ["Show voice controls"])
        click_button(cdp, session, ["End voice"])
    wait_for(
        cdp,
        session,
        "(() => { const s = globalThis.__voiceE2E?.status(); return s?.mode === 'offline' ? s?.voiceClosed : s?.voiceClosed && s?.providerFinalized; })()",
        min(timeout_seconds, 60),
        "voice session close and provider finalization",
    )
    cdp.evaluate("globalThis.__voiceE2E.stop()", session=session)
    return export_capture(cdp, session)


def export_capture(cdp: CDP, session: str) -> dict[str, object]:
    """Freeze once; chunking must not repeatedly stringify minutes of PCM."""
    cdp.evaluate(
        "void (globalThis.__voiceE2EExportJson = JSON.stringify(globalThis.__voiceE2E.export()))", session=session
    )
    try:
        total_chars = int(cast("float", cdp.evaluate("globalThis.__voiceE2EExportJson.length", session=session)))
        if total_chars <= 0 or total_chars > 256_000_000:
            raise RuntimeError("Voice capture exceeds the 256 MB export bound")
        chunks: list[str] = []
        for offset in range(0, total_chars, 40000):
            value = cdp.evaluate(f"globalThis.__voiceE2EExportJson.slice({offset}, {offset + 40000})", session=session)
            chunks.append(str(value or ""))
        return cast("dict[str, object]", json.loads("".join(chunks)))
    finally:
        with contextlib.suppress(Exception):
            cdp.evaluate("delete globalThis.__voiceE2EExportJson", session=session)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("authenticated", "offline"), default="authenticated")
    parser.add_argument("--scenario-json", type=Path)
    parser.add_argument("--workspace", type=Path, default=Path(".voice-e2e-workspace"))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--cdp", default="http://127.0.0.1:9222")
    parser.add_argument("--url", default="", help="Existing local app URL; otherwise launch isolated server")
    parser.add_argument("--port", type=int, default=8876)
    parser.add_argument("--gap-ms", type=int, default=0, help="Offline-only injected audio gap negative control")
    parser.add_argument("--expect-gap-ms", type=int, default=0, help="Offline-only asserted injected gap duration")
    parser.add_argument("--max-seconds", type=int, default=900)
    parser.add_argument("--minimum-active-span-seconds", type=float, default=300)
    parser.add_argument(
        "--rendered-speech-p95-ms",
        dest="rendered_speech_p95_ms",
        type=float,
        default=250,
        help="Maximum per-minute p95 packet-arrival-to-render age for active speech samples (not acoustic latency)",
    )
    parser.add_argument(
        "--rendered-speech-peak-ms",
        dest="rendered_speech_peak_ms",
        type=float,
        default=1000,
        help="Maximum packet-arrival-to-render age for active speech samples (not acoustic latency)",
    )
    args = parser.parse_args()
    if (
        not 30 <= args.max_seconds <= 900
        or not 0 <= args.gap_ms <= 5000
        or not 0 <= args.expect_gap_ms <= 5000
        or args.minimum_active_span_seconds < 0
        or args.rendered_speech_p95_ms <= 0
        or args.rendered_speech_peak_ms <= 0
    ):
        parser.error("invalid duration, gap, or sample-age bounds")
    if args.mode == "authenticated" and (args.config is None or not args.config.is_file()):
        parser.error("Authenticated mode requires an explicit local --config path")
    if args.mode == "authenticated" and args.scenario_json is None:
        parser.error("Authenticated mode requires an explicit local --scenario-json file")
    if args.mode == "offline" and args.config is not None:
        parser.error("Offline mode never accepts an application config")
    if args.expect_gap_ms and (args.mode != "offline" or args.gap_ms != args.expect_gap_ms):
        parser.error("expect-gap-ms requires offline mode with the same --gap-ms value")
    cdp_base = loopback_url(args.cdp, allowed_schemes={"http", "https"})
    app_url = loopback_url(args.url, allowed_schemes={"http", "https"}) if args.url else ""
    scenario = (
        json.loads(args.scenario_json.read_text())
        if args.scenario_json
        else {
            "id": "offline-default",
            "rounds": [
                {"id": "offline-round-1", "text": "Please say hello.", "expect_text": "The audio test is complete."}
            ],
        }
    )
    if not isinstance(scenario, dict):
        parser.error("scenario JSON must be an object")
    scenario = cast("dict[str, object]", scenario)
    artifacts = args.artifacts.expanduser().resolve()
    artifacts.mkdir(parents=True, exist_ok=True, mode=0o700)
    artifacts.chmod(0o700)
    fixtures = artifacts / "fixtures"
    fixtures.mkdir(mode=0o700)
    _, rounds = prepare_scenario(fixtures, scenario, args.mode)
    planned_seconds = sum(numeric(item.get("wait_after_seconds")) for item in rounds)
    planned_seconds += 20 * len(rounds) + sum(
        wav_seconds(fixtures / f"round-{index}.wav") for index in range(1, len(rounds) + 1)
    )
    if args.mode == "authenticated" and planned_seconds > args.max_seconds:
        parser.error(
            f"scenario needs at least {planned_seconds:.0f} seconds; raise --max-seconds within its 900 second cap"
        )
    if args.mode == "offline":
        # The existing fixture server has one scripted answer; use local fixture preparation.
        from examples.live.browser_e2e.artifacts import REPLY_TEXT

        for index in range(1, len(rounds) + 1):
            shutil.copyfile(
                fixtures / f"round-{index}.wav", fixtures / ("caller.wav" if index == 1 else f"round-{index}.wav")
            )
        speech(fixtures / "reply.wav", REPLY_TEXT)
        (fixtures / "fixture.json").write_text(
            json.dumps({"caller_text": str(rounds[0]["text"]), "reply_text": REPLY_TEXT})
        )
    else:
        auth_workspace = args.workspace.expanduser().resolve()
        auth_workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        sentinel_path = auth_workspace / "voice-e2e-sentinel.txt"
        if sentinel_path.exists():
            if sentinel_path.read_text(encoding="utf-8") != f"{SENTINEL}\n":
                parser.error("workspace voice-e2e-sentinel.txt exists with unexpected contents")
        else:
            sentinel_path.write_text(f"{SENTINEL}\n", encoding="utf-8")
            sentinel_path.chmod(0o600)
    configuration = {
        "mode": args.mode,
        "scenarioId": str(scenario.get("id", "scenario"))[:100],
        "rounds": [{key: item[key] for key in ("id", "name", "load")} for item in rounds],
        "startedAtUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    process: subprocess.Popen[str] | None = None
    server_log_path = artifacts / "server.log"
    server_log = server_log_path.open("w", encoding="utf-8")
    server_log_path.chmod(0o600)
    browser_context = ""
    target_id = ""
    cdp: CDP | None = None
    session = ""
    stage = "starting local server"
    try:
        if not app_url:
            command = [
                sys.executable,
                "-m",
                "examples.live.browser_e2e.server",
                "--fixtures",
                str(fixtures),
                "--workspace",
                str(args.workspace.expanduser().resolve()),
                "--port",
                str(args.port),
            ]
            if args.mode == "authenticated":
                command.extend(["--authenticated", "--config", str(args.config.expanduser().resolve())])
            if args.gap_ms:
                if args.mode != "offline":
                    parser.error("gap-ms is only valid in offline mode")
                command.extend(["--gap-ms", str(args.gap_ms)])
            server_env = os.environ.copy()
            if args.mode == "offline":
                isolated_config = artifacts / "isolated-config"
                isolated_data = artifacts / "isolated-data"
                isolated_config.mkdir(mode=0o700)
                isolated_data.mkdir(mode=0o700)
                server_env.update(
                    {
                        "XDG_CONFIG_HOME": str(isolated_config),
                        "XDG_DATA_HOME": str(isolated_data),
                    }
                )
            process = subprocess.Popen(
                command,
                cwd=Path(__file__).resolve().parents[3],
                env=server_env,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            app_url = f"http://127.0.0.1:{args.port}"
            wait_http(app_url, process)
        stage = "validating local test routes"
        verify_capture_routes(app_url)
        stage = "connecting to loopback Chrome CDP"
        endpoint = read_json(f"{cdp_base}/json/version")
        ws_url = loopback_websocket(str(endpoint.get("webSocketDebuggerUrl", "")))
        cdp = CDP(ws_url)
        context = cdp.call("Target.createBrowserContext", {"disposeOnDetach": True})
        browser_context = str(context["browserContextId"])
        target = cdp.call("Target.createTarget", {"url": "about:blank", "browserContextId": browser_context})
        target_id = str(target["targetId"])
        attached = cdp.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        session = str(attached["sessionId"])
        safe_json_file(
            artifacts / "browser-target.json",
            {"targetId": target_id, "browserContextId": browser_context, "appOrigin": app_url},
        )
        cdp.call("Target.activateTarget", {"targetId": target_id})
        cdp.call("Page.enable", session=session)
        cdp.call("Runtime.enable", session=session)
        cdp.call("Page.bringToFront", session=session)
        source = init_source(rounds, args.mode, args.max_seconds)
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": source}, session=session)
        cdp.call("Page.navigate", {"url": app_url}, session=session)
        stage = "loading local app"
        wait_for(cdp, session, "document.readyState === 'complete'", 30, "local app load")
        stage = "voice scenario execution"
        capture = scenario_capture(cdp, session, rounds, args.max_seconds)
        events = cast("list[dict[str, object]]", capture.get("events", []))
        # Attach scenario metadata while preserving the raw time-ordered event stream.
        for event in events:
            if event.get("type") == "fixture.start":
                index = int(numeric(event.get("round")))
                if 1 <= index <= len(rounds):
                    event.update({key: rounds[index - 1][key] for key in ("id", "name", "load")})
        capture["scenario"] = configuration
        app = cast("dict[str, object]", capture.get("environment", {}))
        app["runnerWorktreeRevision"] = (
            subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=Path(__file__).resolve().parents[3],
                check=False,
                capture_output=True,
                text=True,
            ).stdout.strip()
            or "unavailable"
        )
        server_metadata = read_json(f"{app_url}/__voice-e2e/meta")
        app["serverRevision"] = server_metadata.get("sourceRevision", "unavailable")
        app["serverTreeDirty"] = server_metadata.get("sourceTreeDirty")
        app["packageVersion"] = server_metadata.get("packageVersion", "unknown")
        app["sourceModuleSha256"] = server_metadata.get("moduleSha256", {})
        build_metadata = cast(
            "dict[str, object]",
            cdp.evaluate(
                """(async () => {
                  const appPaths = [...document.querySelectorAll('script[src],link[rel="stylesheet"][href]')]
                    .map(item => item.src || item.href).filter(Boolean)
                    .map(src => new URL(src, location.href)).filter(url => url.origin === location.origin)
                    .map(url => url.pathname).slice(0, 16);
                  const paths = [...new Set(appPaths.concat(['/assets/live-playback.js', '/assets/live-capture.js',
                    '/assets/voice-e2e-capture.js']))];
                  const assets = {};
                  for (const path of paths) {
                    try {
                      const response = await fetch(path);
                      if (!response.ok) continue;
                      const digest = await crypto.subtle.digest('SHA-256', await response.arrayBuffer());
                      assets[path] = [...new Uint8Array(digest)].map(value => value.toString(16).padStart(2, '0')).join('');
                    } catch {}
                  }
                  return assets;
                })()""",
                session=session,
            ),
        )
        app["assetSha256"] = build_metadata
        metadata = {"scenario": configuration, "capture": "capture.json", "report": "report.json"}
        safe_json_file(artifacts / "capture.json", capture)
        if args.mode == "authenticated":
            result = check_authenticated_scenario(
                capture,
                rounds,
                minimum_active_span_seconds=args.minimum_active_span_seconds,
                max_rendered_speech_p95_ms=args.rendered_speech_p95_ms,
                max_rendered_speech_peak_ms=args.rendered_speech_peak_ms,
                require_rendered_speech_telemetry=True,
            )
        else:
            with __import__("wave").open(str(fixtures / "reply.wav"), "rb") as wav:
                reply_samples = wav.getnframes()
            result = check_capture(
                capture,
                mode="offline",
                reply_samples=reply_samples,
                expect_text=REPLY_TEXT,
                expect_gap_ms=args.expect_gap_ms,
            )
        report = {
            **metadata,
            "passed": result["passed"],
            "checks": result["checks"],
            "details": result["details"],
            "mode": args.mode,
        }
        safe_json_file(artifacts / "report.json", report)
        print(
            json.dumps(
                {
                    "passed": result["passed"],
                    "mode": args.mode,
                    "scenarioId": configuration["scenarioId"],
                    "report": str(artifacts / "report.json"),
                }
            )
        )
        raise SystemExit(0 if result["passed"] else 1)
    except (Exception, KeyboardInterrupt) as error:
        safe_json_file(
            artifacts / "failure.json",
            {"stage": stage, "errorType": type(error).__name__, "detail": sanitized_error(error)},
        )
        if cdp is not None and session:
            cleanup: dict[str, object] = {"attempted": False, "voiceClosed": False, "providerFinalized": False}
            with contextlib.suppress(Exception):
                state = cdp.evaluate("globalThis.__voiceE2E?.status() || null", session=session)
                safe_json_file(artifacts / "partial-status.json", state)
            with contextlib.suppress(Exception):
                trace = cdp.evaluate(
                    "JSON.stringify(globalThis.__voiceE2E?.export().events.slice(-300) || [])", session=session
                )
                safe_json_file(artifacts / "partial-events.json", json.loads(str(trace or "[]")))
            with contextlib.suppress(Exception):
                screenshot = cdp.call("Page.captureScreenshot", {"format": "png"}, session=session)
                data = str(screenshot.get("data", ""))
                if data:
                    (artifacts / "failure.png").write_bytes(base64.b64decode(data))
                    (artifacts / "failure.png").chmod(0o600)
            with contextlib.suppress(Exception):
                state = cast(
                    "dict[str, object]", cdp.evaluate("globalThis.__voiceE2E?.status() || {}", session=session)
                )
                cleanup["voiceClosed"] = state.get("voiceClosed") is True
                cleanup["providerFinalized"] = state.get("providerFinalized") is True
                if state.get("microphone") and not state.get("voiceClosed"):
                    cleanup["attempted"] = True
                    try:
                        click_button(cdp, session, ["End voice", "Cancel connection"], timeout=1)
                    except TimeoutError:
                        click_button(cdp, session, ["Show voice controls"], timeout=2)
                        click_button(cdp, session, ["End voice", "Cancel connection"], timeout=2)
                    wait_for(
                        cdp,
                        session,
                        "(() => { const s = globalThis.__voiceE2E?.status(); return s?.mode === 'offline' ? s?.voiceClosed : s?.voiceClosed && s?.providerFinalized; })()",
                        20,
                        "voice cleanup",
                    )
                    state = cast("dict[str, object]", cdp.evaluate("globalThis.__voiceE2E.status()", session=session))
                    cleanup["voiceClosed"] = state.get("voiceClosed") is True
                    cleanup["providerFinalized"] = state.get("providerFinalized") is True
            safe_json_file(artifacts / "cleanup.json", cleanup)
            with contextlib.suppress(Exception):
                cdp.evaluate("globalThis.__voiceE2E?.stop()", session=session)
            with contextlib.suppress(Exception):
                safe_json_file(artifacts / "partial-capture.json", export_capture(cdp, session))
        raise SystemExit(f"Voice browser E2E failed during {stage}; private diagnostics are in {artifacts}") from None
    finally:
        if cdp is not None:
            if target_id:
                with contextlib.suppress(Exception):
                    cdp.call("Target.closeTarget", {"targetId": target_id})
            if browser_context:
                with contextlib.suppress(Exception):
                    cdp.call("Target.disposeBrowserContext", {"browserContextId": browser_context})
            cdp.socket.close()
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
            with contextlib.suppress(OSError):
                shutil.copyfile(fixtures / "server-events.jsonl", artifacts / "server-events.jsonl")
                (artifacts / "server-events.jsonl").chmod(0o600)
        server_log.close()


if __name__ == "__main__":
    main()
