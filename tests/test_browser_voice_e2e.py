"""Artifact validation must distinguish a real interruption from terminal EOF."""

import ast
import base64
import copy
import json
import os
import shutil
import struct
import subprocess
import wave
from collections.abc import Callable
from functools import partial
from pathlib import Path
from typing import cast

import pytest
from examples.live.browser_e2e.artifacts import check_authenticated_scenario
from examples.live.browser_e2e.artifacts import check_capture
from examples.live.browser_e2e.artifacts import extract_report
from examples.live.browser_e2e.artifacts import timing_metrics
from examples.live.browser_e2e.artifacts import unpack


def block(frame: int = 0, *, silent: bool = False) -> dict[str, object]:
    return {
        "contextId": 0,
        "frame": frame,
        "observedFrame": frame,
        "sampleOffset": frame,
        "sequence": frame // 4,
        "samples": 4,
        "sampleRate": 48000,
        "peak": 0 if silent else 0.05,
        "pcm16": base64.b64encode(struct.pack("<4h", *([0 if silent else 1000] * 4))).decode(),
    }


def baseline() -> dict[str, object]:
    return {
        "schema": "ngn-browser-voice-e2e/v2",
        "mode": "offline",
        "stopped": True,
        "omitted": 0,
        "input": [block()],
        "output": [block()],
        "health": [{"receivedSamples": 4, "playedSamples": 4, "underruns": 1, "droppedSamples": 0}],
        "events": [
            {"type": "socket.receive", "monotonicMs": 100},
            {"type": "socket.receive", "monotonicMs": 120},
            {"type": "live.status", "status": "closed"},
            {
                "type": "ui.voice",
                "state": [{"selector": ".voice-controls", "phase": "connected", "text": "LiveLiveStop"}],
            },
            {
                "type": "recorder.capture-start",
                "contextId": 0,
                "frame": 0,
                "sampleOffset": 0,
                "sequence": 0,
                "monotonicMs": 20,
            },
            {
                "type": "recorder.capture-stop",
                "contextId": 0,
                "finalFrame": 4,
                "samplesCaptured": 4,
                "blocksCaptured": 1,
                "armed": True,
                "monotonicMs": 200,
            },
        ],
    }


def received_packet(sequence: int, samples: tuple[int, ...], *, offset: int | None = None) -> dict[str, object]:
    data = struct.pack(f"<{len(samples)}h", *samples)
    return {
        "sequence": sequence,
        "captureOrder": sequence + 10,
        "sampleOffset": sequence * 4 if offset is None else offset,
        "receivedSamples": (sequence + 1) * 4,
        "samples": len(samples),
        "bytes": len(data),
        "sampleRate": 24000,
        "contextId": 0,
        "epoch": 1,
        "audioTime": sequence / 300,
        "pcm16": base64.b64encode(data).decode(),
    }


def with_received(report: dict[str, object]) -> dict[str, object]:
    packets = [received_packet(0, (11, 12, 13, 14)), received_packet(1, (21, 22, 23, 24))]
    report["received"] = packets
    report["receivedPcm"] = {
        "sampleFormat": "s16le",
        "sampleRate": 24000,
        "packetCount": 2,
        "capturedPackets": 2,
        "samples": 8,
        "capturedSamples": 8,
        "capturedBytes": 16,
        "omittedPackets": 0,
        "omittedBytes": 0,
    }
    return report


def object_record(report: dict[str, object], key: str, index: int | None = None) -> dict[str, object]:
    value = report[key]
    if index is not None:
        assert isinstance(value, list)
        value = value[index]
    assert isinstance(value, dict)
    return cast("dict[str, object]", value)


def test_unpack_writes_complete_received_network_pcm_and_strips_all_bulk_audio(tmp_path: Path) -> None:
    source = tmp_path / "capture.json"
    source.write_text(json.dumps(with_received(baseline())))
    destination = tmp_path / "unpacked"
    unpack(source, destination)
    received_path = destination / "speaker-received.wav"
    with wave.open(str(received_path), "rb") as recording:
        assert recording.getframerate() == 24000
        assert recording.getnchannels() == 1
        assert recording.readframes(8) == struct.pack("<8h", 11, 12, 13, 14, 21, 22, 23, 24)
    if os.name != "nt":
        assert received_path.stat().st_mode & 0o777 == 0o600
    metrics = json.loads((destination / "metrics.json").read_text())
    assert metrics["receivedPcm"] == {
        "status": "complete",
        "complete": True,
        "file": "speaker-received.wav",
        "sampleRate": 24000,
        "packets": 2,
        "samples": 8,
        "bytes": 16,
    }
    trace = json.loads((destination / "trace.json").read_text())
    for channel in ("input", "output", "received"):
        assert all("pcm16" not in item for item in trace[channel])
    assert trace["received"][0]["sequence"] == 0


def test_unpack_marks_legacy_capture_as_not_captured_without_received_wav(tmp_path: Path) -> None:
    source = tmp_path / "legacy.json"
    source.write_text(json.dumps(baseline()))
    destination = tmp_path / "unpacked"
    destination.mkdir()
    (destination / "speaker-received.wav").write_bytes(b"stale artifact")
    unpack(source, destination)
    assert not (destination / "speaker-received.wav").exists()
    metrics = json.loads((destination / "metrics.json").read_text())
    assert metrics["receivedPcm"] == {"status": "not-captured", "complete": False, "file": ""}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda packet: packet.update(contextId=None),
        lambda packet: packet.update(contextId=1),
        lambda packet: packet.update(captureOrder=None),
        lambda packet: packet.update(captureOrder=0),
    ],
)
def test_unpack_rejects_unknown_mixed_or_invalid_receive_context_and_order(
    tmp_path: Path, mutate: Callable[[dict[str, object]], None]
) -> None:
    report = with_received(baseline())
    mutate(object_record(report, "received", 1))
    destination = tmp_path / "unpacked"
    destination.mkdir()
    stale = destination / "speaker-received.wav"
    stale.write_bytes(b"stale artifact")
    source = tmp_path / "capture.json"
    source.write_text(json.dumps(report))
    unpack(source, destination)
    assert not stale.exists()
    metrics = json.loads((destination / "metrics.json").read_text())
    assert metrics["receivedPcm"]["complete"] is False
    assert metrics["receivedPcm"]["status"] == "incomplete"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda report: object_record(report, "receivedPcm").update(omittedPackets=1),
        lambda report: object_record(report, "received", 1).update(sequence=3),
        lambda report: object_record(report, "received", 0).update(bytes=99),
        lambda report: object_record(report, "receivedPcm").update(capturedBytes=14),
        lambda report: object_record(report, "received", 0).update(pcm16="%%%"),
        lambda report: object_record(report, "receivedPcm").update(capturedPackets=1),
    ],
)
def test_unpack_never_exports_incomplete_or_invalid_received_pcm(
    tmp_path: Path, mutate: Callable[[dict[str, object]], None]
) -> None:
    report = with_received(baseline())
    mutate(report)
    source = tmp_path / "capture.json"
    source.write_text(json.dumps(report))
    destination = tmp_path / "unpacked"
    unpack(source, destination)
    assert not (destination / "speaker-received.wav").exists()
    metrics = json.loads((destination / "metrics.json").read_text())
    assert metrics["receivedPcm"]["status"] == "incomplete"
    assert metrics["receivedPcm"]["complete"] is False
    trace = json.loads((destination / "trace.json").read_text())
    assert all("pcm16" not in item for item in trace["received"])


def test_unpack_keeps_callback_order_audio_when_clock_overlaps(tmp_path: Path) -> None:
    report = baseline()
    first, second = block(4), block(0)
    first["pcm16"] = base64.b64encode(struct.pack("<4h", 1, 2, 3, 4)).decode()
    second["pcm16"] = base64.b64encode(struct.pack("<4h", 5, 6, 7, 8)).decode()
    report["output"] = [first, second]
    source = tmp_path / "capture.json"
    source.write_text(json.dumps(report))
    destination = tmp_path / "unpacked"
    unpack(source, destination)
    with wave.open(str(destination / "speaker-callback-order-0.wav"), "rb") as recording:
        assert recording.readframes(8) == struct.pack("<8h", 1, 2, 3, 4, 5, 6, 7, 8)
    assert not (destination / "speaker-rendered-0.wav").exists()
    metrics = json.loads((destination / "metrics.json").read_text())
    assert metrics["checks"]["recorderContinuous"] is False
    assert metrics["outputs"][0]["file"] == ""
    assert metrics["outputs"][0]["callbackOrderFile"] == "speaker-callback-order-0.wav"


def test_unpack_writes_rendered_audio_only_for_continuous_capture(tmp_path: Path) -> None:
    source = tmp_path / "capture.json"
    source.write_text(json.dumps(baseline()))
    destination = tmp_path / "unpacked"
    unpack(source, destination)
    assert (destination / "speaker-rendered-0.wav").read_bytes() == (
        destination / "speaker-callback-order-0.wav"
    ).read_bytes()
    metrics = json.loads((destination / "metrics.json").read_text())
    assert metrics["checks"]["recorderContinuous"] is True


def authenticated_scenario_capture() -> dict[str, object]:
    report = baseline()
    report["mode"] = "authenticated"
    report["input"] = [
        {**block(), "monotonicMs": 1100},
        {**block(), "monotonicMs": 5100},
    ]
    report["output"] = [
        {**block(), "sampleRate": 4, "monotonicMs": 3000, "contextTimeAtDelivery": 0},
        {**block(4), "sampleRate": 4, "monotonicMs": 6000, "contextTimeAtDelivery": 1},
    ]
    report["health"] = [
        {"receivedSamples": 4, "playedSamples": 4, "underruns": 0, "droppedSamples": 0, "bufferedMs": 0},
        {"receivedSamples": 8, "playedSamples": 8, "underruns": 0, "droppedSamples": 0, "bufferedMs": 0},
    ]
    events = cast("list[dict[str, object]]", report["events"])
    events[:] = [event for event in events if event.get("type") != "socket.receive"]
    for event in events:
        if event.get("type") == "recorder.capture-start":
            event["frame"] = 48000
            event["observedFrame"] = 48000
        elif event.get("type") == "recorder.capture-stop":
            event["finalFrame"] = 48008
            event["samplesCaptured"] = 8
            event["blocksCaptured"] = 2
    events.extend(
        [
            {"type": "measurement.start", "monotonicMs": 20},
            {
                "type": "recorder.capture-start",
                "contextId": 0,
                "frame": 0,
                "sampleOffset": 0,
                "sequence": 0,
                "monotonicMs": 20,
            },
            {"type": "fixture.start", "round": 1, "monotonicMs": 1000, "duration": 0.1, "delayMs": 0},
            {
                "type": "live.event",
                "eventType": "transcript",
                "speaker": "user",
                "text": "Say hello",
                "monotonicMs": 1100,
            },
            {
                "type": "live.event",
                "eventType": "transcript",
                "speaker": "assistant",
                "text": "Hello there.",
                "monotonicMs": 3200,
            },
            {"type": "fixture.start", "round": 2, "monotonicMs": 5000, "duration": 0.1, "delayMs": 0},
            {
                "type": "live.event",
                "eventType": "transcript",
                "speaker": "user",
                "text": "Calculate seventeen times twenty three.",
                "monotonicMs": 5100,
            },
            {
                "type": "live.event",
                "eventType": "tool_call",
                "name": "calculator",
                "call_id": "call-1",
                "monotonicMs": 5400,
            },
            {
                "type": "live.event",
                "eventType": "tool_execution_started",
                "name": "calculator",
                "call_id": "call-1",
                "monotonicMs": 5500,
            },
            {
                "type": "live.event",
                "eventType": "delegation",
                "delegation_id": "delegation-1",
                "status": "queued",
                "monotonicMs": 5600,
            },
            {
                "type": "live.event",
                "eventType": "tool_result",
                "name": "calculator",
                "call_id": "call-1",
                "monotonicMs": 5800,
            },
            {
                "type": "live.event",
                "eventType": "delegation",
                "delegation_id": "delegation-1",
                "status": "working",
                "monotonicMs": 6000,
            },
            {
                "type": "live.event",
                "eventType": "delegation",
                "delegation_id": "delegation-1",
                "status": "completed",
                "monotonicMs": 6900,
            },
            {
                "type": "live.event",
                "eventType": "transcript",
                "speaker": "assistant",
                "text": "The result is three hundred and ninety one.",
                "monotonicMs": 7200,
            },
            {
                "type": "live.event",
                "eventType": "status",
                "text": "Live session closed. Finalization confirmed.",
                "monotonicMs": 9000,
            },
            {"type": "live.status", "status": "closed", "monotonicMs": 9000},
            {
                "type": "ui.voice",
                "state": [{"selector": ".voice-controls", "phase": "connected", "text": "LiveLiveStop"}],
                "monotonicMs": 1000,
            },
            {
                "type": "recorder.capture-stop",
                "contextId": 0,
                "finalFrame": 8,
                "samplesCaptured": 8,
                "blocksCaptured": 2,
                "armed": True,
                "monotonicMs": 9100,
            },
        ]
    )
    return report


def test_latency_mapping_ignores_connection_warmup_epoch_but_not_recording_resets() -> None:
    report = baseline()
    report["events"] = [
        {"type": "measurement.start", "monotonicMs": 20},
        {"type": "socket.receive", "path": "/api/live/sessions/owned/audio", "receivedSamples": 400, "monotonicMs": 30},
    ]
    report["health"] = [
        {"epoch": 0, "contextId": 0, "monotonicMs": 10, "receivedSamples": 0, "playedSamples": 0},
        {
            "epoch": 1,
            "contextId": 0,
            "monotonicMs": 50,
            "receivedSamples": 400,
            "playedSamples": 100,
            "bufferedMs": 12.5,
            "renderedAt": 0.01,
            "audioTime": 0.01,
        },
    ]
    timing = timing_metrics(report)
    assert timing["sampleAgeAvailable"] is True
    windows = cast("list[dict[str, object]]", timing["minuteWindows"])
    assert windows[0]["healthSamples"] == 1
    assert cast("dict[str, object]", windows[0]["sampleAgeMs"])["max"] == 20
    cast("list[dict[str, object]]", report["health"]).append(
        {"epoch": 2, "contextId": 0, "monotonicMs": 60, "playedSamples": 100}
    )
    assert timing_metrics(report)["sampleAgeAvailable"] is False


def rendered_speech_report(*, audible: bool = True, source_start: int = 0) -> dict[str, object]:
    pcm = struct.pack("<4h", 0, 1000, -1200, 0)
    output = struct.pack("<8h", *([1000] * 8)) if audible else struct.pack("<8h", *([0] * 8))
    return {
        "contexts": [{"id": 0, "sampleRate": 48000}],
        "events": [
            {
                "type": "playback.submit",
                "contextId": 0,
                "epoch": 2,
                "samples": 4,
                "bytes": 8,
                "submittedSamples": 4,
                "receiveMonotonicMs": 100,
            },
            {
                "type": "playback.rendered",
                "contextId": 0,
                "epoch": 2,
                "sourceStart": source_start,
                "samples": 4,
                "sampleRate": 24000,
                "firstActiveSample": 1,
                "lastActiveSample": 2,
                "activeSamples": 2,
                "activeSpans": [[1, 3]],
                "contextSampleRate": 48000,
                "at": 1.0,
                "until": 1 + 3 / 24000,
                "contextTimeAtDelivery": 1.0,
                "outputTimestamp": {"contextTime": 1.0, "performanceTime": 500},
                "monotonicMs": 500,
            },
        ],
        "output": [
            {
                "contextId": 0,
                "frame": 48000,
                "observedFrame": 48000,
                "sampleOffset": 0,
                "sequence": 0,
                "samples": 8,
                "sampleRate": 48000,
                "contextTimeAtDelivery": 1.0,
                "outputTimestamp": {"contextTime": 1.0, "performanceTime": 500},
                "pcm16": base64.b64encode(output).decode(),
            }
        ],
        "sourcePcm": base64.b64encode(pcm).decode(),
    }


def test_rendered_speech_age_fails_delayed_voiced_pcm_but_ignores_quiet_queue() -> None:
    report = rendered_speech_report()
    result = timing_metrics(report)
    speech = cast("dict[str, object]", result["renderedSpeechAge"])
    assert speech["available"] is True
    assert speech["p95Within250Ms"] is False
    assert speech["withinBounds"] is False
    assert speech["p95Ms"] == pytest.approx(400.0833, abs=0.001)
    assert speech["observations"] == 2

    quiet = rendered_speech_report()
    cast("list[dict[str, object]]", quiet["events"]).append(
        {
            "type": "playback.rendered",
            "contextId": 0,
            "epoch": 2,
            "sourceStart": 4,
            "samples": 4,
            "firstActiveSample": -1,
            "lastActiveSample": -1,
            "activeSamples": 0,
            "activeSpans": [],
        }
    )
    quiet_result = cast("dict[str, object]", timing_metrics(quiet)["renderedSpeechAge"])
    assert quiet_result["available"] is True
    assert quiet_result["silentBlocks"] == 1


@pytest.mark.parametrize(
    ("report", "expected_key"),
    [
        (rendered_speech_report(source_start=8), "unmappedBlocks"),
        (rendered_speech_report(audible=False), "unconfirmedPostVolumeBlocks"),
    ],
)
def test_rendered_speech_age_fails_closed_without_source_or_post_volume_mapping(
    report: dict[str, object], expected_key: str
) -> None:
    result = cast("dict[str, object]", timing_metrics(report)["renderedSpeechAge"])
    assert result["available"] is False
    assert result["withinBounds"] is False
    assert result[expected_key] == 1


def test_rendered_speech_age_fails_closed_on_invalid_source_coordinates() -> None:
    report = rendered_speech_report()
    rendered = cast("list[dict[str, object]]", report["events"])[1]
    rendered.pop("sourceStart")
    result = cast("dict[str, object]", timing_metrics(report)["renderedSpeechAge"])
    assert result["available"] is False
    assert result["invalidBlocks"] == 1
    assert result["withinBounds"] is False


def test_rendered_speech_age_rejects_missing_render_report_coverage() -> None:
    report = rendered_speech_report()
    events = cast("list[dict[str, object]]", report["events"])
    second = events[-1].copy()
    second["sourceStart"] = 8
    cast("list[dict[str, object]]", report["events"]).append(second)
    result = cast("dict[str, object]", timing_metrics(report)["renderedSpeechAge"])
    assert result["available"] is False
    assert result["coverageErrors"] == 1


@pytest.mark.parametrize("bad_context", [[], True])
def test_rendered_speech_age_fails_closed_on_invalid_submission_context(bad_context: object) -> None:
    report = rendered_speech_report()
    cast("list[dict[str, object]]", report["events"]).append(
        {
            "type": "playback.submit",
            "contextId": bad_context,
            "epoch": 2,
            "samples": 1,
            "bytes": 2,
            "submittedSamples": 5,
            "receiveMonotonicMs": 101,
        }
    )
    result = cast("dict[str, object]", timing_metrics(report)["renderedSpeechAge"])
    assert result["available"] is False
    assert result["invalidSubmissionEvents"] == 1


def test_rendered_speech_age_rejects_mismatched_output_sample_rate() -> None:
    report = rendered_speech_report()
    rendered = cast("list[dict[str, object]]", report["events"])[1]
    rendered["contextSampleRate"] = 1
    result = cast("dict[str, object]", timing_metrics(report)["renderedSpeechAge"])
    assert result["available"] is False
    assert result["invalidBlocks"] == 1


def test_literal_cdp_evaluate_javascript_parses_without_execution() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required to syntax-check the browser runner's CDP expressions")
    runner_path = Path(__file__).parents[1] / "examples/live/browser_e2e/cdp_runner.py"
    tree = ast.parse(runner_path.read_text())
    expressions = [
        [call.lineno, call.args[0].value]
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "evaluate"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
    ]
    assert expressions
    checker = (
        'const fs=require("fs"); for(const [line,code] of JSON.parse(fs.readFileSync(0,"utf8"))) '
        '{ try { new Function("return (" + code + ")"); } '
        "catch(error) { console.error(`line ${line}: ${error.message}`); process.exitCode=1; } }"
    )
    subprocess.run([node, "-e", checker], input=json.dumps(expressions), text=True, check=True, capture_output=True)


def authenticated_age_report(*, speech_received_ms: float) -> dict[str, object]:
    report = authenticated_scenario_capture()
    report["contexts"] = [{"id": 0, "sampleRate": 48000}]
    output = cast("list[dict[str, object]]", report["output"])
    for index, block_item in enumerate(output):
        block_item["sampleRate"] = 48000
        block_item["frame"] = 48000 + index * 4
        block_item["observedFrame"] = 48000 + index * 4
        block_item["sampleOffset"] = index * 4
        block_item["audioTime"] = (48000 + index * 4) / 48000
        block_item["contextTimeAtDelivery"] = block_item["audioTime"]
        block_item["outputTimestamp"] = {
            "contextTime": block_item["audioTime"],
            "performanceTime": 6000 if index == 1 else block_item["monotonicMs"],
        }
    events = cast("list[dict[str, object]]", report["events"])
    events.extend(
        [
            {"type": "socket.receive", "path": "/audio", "receivedSamples": 4, "monotonicMs": 4500},
            {
                "type": "playback.submit",
                "contextId": 0,
                "epoch": 2,
                "samples": 4,
                "bytes": 8,
                "submittedSamples": 4,
                "receiveMonotonicMs": speech_received_ms,
            },
            {
                "type": "playback.rendered",
                "contextId": 0,
                "epoch": 2,
                "sourceStart": 0,
                "samples": 4,
                "firstActiveSample": 1,
                "lastActiveSample": 2,
                "activeSamples": 2,
                "activeSpans": [[1, 3]],
                "at": 48004 / 48000,
                "until": 48004 / 48000 + 3 / 24000,
                "contextSampleRate": 48000,
                "contextTimeAtDelivery": 48004 / 48000,
                "outputTimestamp": {"contextTime": 48004 / 48000, "performanceTime": 6000},
                "monotonicMs": 6000,
            },
        ]
    )
    report["health"] = [
        {
            "epoch": 2,
            "contextId": 0,
            "monotonicMs": 6000,
            "receivedSamples": 4,
            "playedSamples": 4,
            "droppedSamples": 0,
            "bufferedMs": 20,
            "audioTime": 48004 / 48000,
            "renderedAt": 48004 / 48000,
            "outputTimestamp": {"contextTime": 48004 / 48000, "performanceTime": 6000},
        }
    ]
    return report


def test_authenticated_scenario_gates_rendered_speech_not_old_quiet_queue_age() -> None:
    scenario: list[dict[str, object]] = [
        {"name": "greeting", "expect_text": "Hello there."},
        {
            "name": "tool-delegation",
            "expect_text": "three hundred and ninety one",
            "require_tool_call": True,
            "require_delegation": True,
        },
    ]
    timely = authenticated_age_report(speech_received_ms=5999)
    passing = check_authenticated_scenario(
        timely,
        scenario,
        require_rendered_speech_telemetry=True,
        max_rendered_speech_p95_ms=250,
        max_rendered_speech_peak_ms=1000,
    )
    assert passing["passed"] is True
    checks = cast("dict[str, bool]", passing["checks"])
    assert checks["renderedSpeechTelemetryAvailable"] is True
    assert checks["renderedSpeechP95WithinBound"] is True
    assert checks["renderedSpeechPeakWithinBound"] is True
    timing = cast("dict[str, object]", cast("dict[str, object]", passing["details"])["timing"])
    assert cast("dict[str, object]", cast("list[object]", timing["minuteWindows"])[0])["sampleAgeMs"] == {
        "samples": 1,
        "p95": 1500,
        "max": 1500,
    }

    delayed = check_authenticated_scenario(
        authenticated_age_report(speech_received_ms=5700),
        scenario,
        require_rendered_speech_telemetry=True,
        max_rendered_speech_p95_ms=250,
        max_rendered_speech_peak_ms=1000,
    )
    assert cast("dict[str, bool]", delayed["checks"])["renderedSpeechP95WithinBound"] is False


def test_terminal_eof_is_not_an_in_speech_glitch() -> None:
    result = check_capture(baseline(), mode="offline", reply_samples=4)
    assert result["passed"] is True
    assert cast("dict[str, object]", result["details"])["terminalUnderruns"] == 1


def test_authenticated_scenario_requires_each_answer_tool_and_delegation() -> None:
    scenario: list[dict[str, object]] = [
        {"name": "greeting", "expect_text": "Hello there."},
        {
            "name": "tool-delegation",
            "expect_text": "three hundred and ninety one",
            "require_tool_call": True,
            "require_delegation": True,
            "load": "tool + delegation",
        },
    ]
    report = authenticated_scenario_capture()
    result = check_authenticated_scenario(report, scenario)
    assert result["passed"] is True
    assert cast("dict[str, bool]", result["checks"])["round1ExpectedResponse"] is True
    assert cast("dict[str, bool]", result["checks"])["round2ExpectedResponse"] is True
    details = cast("dict[str, object]", result["details"])
    latency_by_load = cast("dict[str, dict[str, object]]", details["latencyMsByLoad"])
    assert latency_by_load["tool + delegation"]["samples"] == 1

    no_answer = copy.deepcopy(report)
    events = cast("list[dict[str, object]]", no_answer["events"])
    events[:] = [event for event in events if event.get("text") != "The result is three hundred and ninety one."]
    result = check_authenticated_scenario(no_answer, scenario)
    assert result["passed"] is False
    assert cast("dict[str, bool]", result["checks"])["round2ExpectedResponse"] is False

    no_tool_result = copy.deepcopy(report)
    events = cast("list[dict[str, object]]", no_tool_result["events"])
    events[:] = [event for event in events if event.get("eventType") != "tool_result"]
    result = check_authenticated_scenario(no_tool_result, scenario)
    assert result["passed"] is False
    assert cast("dict[str, bool]", result["checks"])["round2ExpectedResponse"] is False


def test_authenticated_natural_network_pause_is_reported_not_called_jitter() -> None:
    scenario: list[dict[str, object]] = [{"name": "greeting", "expect_text": "Hello there."}]
    report = authenticated_scenario_capture()
    events = cast("list[dict[str, object]]", report["events"])
    events.extend(
        [
            {"type": "socket.receive", "monotonicMs": 10000},
            {"type": "socket.receive", "monotonicMs": 12500},
        ]
    )
    assert check_authenticated_scenario(report, scenario)["passed"] is True

    health = cast("list[dict[str, object]]", report["health"])
    health.append(
        {
            "receivedSamples": 12,
            "playedSamples": 8,
            "underruns": 1,
            "droppedSamples": 0,
            "bufferedMs": 25,
            "monotonicMs": 12600,
        }
    )
    result = check_authenticated_scenario(report, scenario)
    assert result["passed"] is False
    assert cast("dict[str, bool]", result["checks"])["noUnclassifiedQueuedUnderruns"] is False


def test_authenticated_delegation_can_be_proved_by_owned_task_lifecycle() -> None:
    scenario: list[dict[str, object]] = [
        {"name": "greeting", "expect_text": "Hello there."},
        {
            "name": "delegated",
            "expect_text": ["five thousand seventeen", "NGN voice E2E 5017"],
            "require_tool_call": True,
            "require_subagent": True,
        },
    ]
    report = authenticated_scenario_capture()
    events = cast("list[dict[str, object]]", report["events"])
    events[:] = [event for event in events if event.get("eventType") != "delegation"]
    for event in events:
        if event.get("speaker") == "assistant" and str(event.get("text", "")).startswith("The result is"):
            event["text"] = "The marker is NGN voice E2E 5017."
    events.extend(
        [
            {
                "type": "live.event",
                "eventType": "delegation",
                "delegation_id": "voice-handoff",
                "status": "queued",
                "monotonicMs": 2000,
            },
            {
                "type": "live.event",
                "eventType": "delegation",
                "delegation_id": "voice-handoff",
                "status": "working",
                "monotonicMs": 2100,
            },
            {
                "type": "live.event",
                "eventType": "delegation",
                "delegation_id": "voice-handoff",
                "status": "completed",
                "monotonicMs": 2200,
            },
            {
                "type": "live.event",
                "eventType": "task_started",
                "run_id": "run-owned",
                "task_id": "task-owned",
                "monotonicMs": 5600,
            },
            {
                "type": "live.event",
                "eventType": "task_completed",
                "run_id": "run-owned",
                "task_id": "task-owned",
                "status": "completed",
                "monotonicMs": 6900,
            },
        ]
    )
    result = check_authenticated_scenario(report, scenario)
    assert result["passed"] is True
    details = cast("dict[str, object]", result["details"])
    round_detail = cast("list[dict[str, object]]", details["scenarioRounds"])[1]
    assert round_detail["delegationCompleted"] is None
    assert round_detail["subagentCompleted"] is True
    assert round_detail["taskLifecycles"] == {"run-owned:task-owned": ["task_completed", "task_started"]}

    unfinished = copy.deepcopy(report)
    events = cast("list[dict[str, object]]", unfinished["events"])
    events[:] = [event for event in events if event.get("eventType") != "task_completed"]
    result = check_authenticated_scenario(unfinished, scenario)
    assert result["passed"] is False


def test_authenticated_empty_queue_underrun_is_reported_as_ambiguous() -> None:
    scenario: list[dict[str, object]] = [{"name": "greeting", "expect_text": "Hello there."}]
    report = authenticated_scenario_capture()
    health = cast("list[dict[str, object]]", report["health"])
    health.append(
        {
            "receivedSamples": 8,
            "playedSamples": 8,
            "underruns": 1,
            "droppedSamples": 0,
            "bufferedMs": 0,
            "monotonicMs": 8500,
            "lastUnderrunAt": 7.2,
        }
    )
    result = check_authenticated_scenario(report, scenario)
    assert result["passed"] is True
    details = cast("dict[str, object]", result["details"])
    assert len(cast("list[object]", details["ambiguousQueueEmptyUnderruns"])) == 1
    assert "does not prove all perceived audio smoothness" in str(details["smoothnessConclusion"])


def test_known_response_gap_fails_baseline_and_passes_negative_control() -> None:
    report = baseline()
    report["health"] = [
        {"receivedSamples": 2, "playedSamples": 2, "underruns": 1, "droppedSamples": 0},
        {"receivedSamples": 4, "playedSamples": 4, "underruns": 2, "droppedSamples": 0},
    ]
    events = cast("list[dict[str, object]]", report["events"])
    events[1]["monotonicMs"] = 320
    assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False
    assert check_capture(report, mode="offline", reply_samples=4, expect_gap_ms=200)["passed"] is True
    assert check_capture(baseline(), mode="offline", reply_samples=4, expect_gap_ms=200)["passed"] is False


def test_raw_clock_jumps_remain_visible_without_claiming_pcm_loss() -> None:
    report = baseline()
    events = cast("list[dict[str, object]]", report["events"])
    events.append(
        {
            "type": "recorder.clock-discontinuity",
            "contextId": 0,
            "fromFrame": 4,
            "toFrame": 8,
            "monotonicMs": 10,
            "phase": "warmup",
        }
    )
    result = check_capture(report, mode="offline", reply_samples=4)
    assert result["passed"] is True
    details = cast("dict[str, object]", result["details"])
    assert details["totalMissingRecorderFrames"] == 0
    assert len(cast("list[object]", details["warmupClockDiscontinuities"])) == 1
    events[-1]["phase"] = "recording"
    events[-1]["monotonicMs"] = 30
    result = check_capture(report, mode="offline", reply_samples=4)
    assert result["passed"] is True
    assert cast("dict[str, object]", result["details"])["renderClockConsistent"] is False
    events.pop()
    report["output"] = [block(), block(8)]
    assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False


def test_authenticated_run_requires_real_delegation_and_finalization() -> None:
    report = baseline()
    report["mode"] = "authenticated"
    assert check_capture(report, mode="authenticated")["passed"] is False
    events = cast("list[dict[str, object]]", report["events"])
    for status in ("queued", "working", "completed"):
        events.append({"type": "live.event", "eventType": "delegation", "delegation_id": "request-1", "status": status})
    events.append({"type": "live.event", "eventType": "status", "text": "Live session closed. Finalization confirmed."})
    events.append(
        {"type": "live.event", "eventType": "transcript", "speaker": "assistant", "text": "Three ninety-one."}
    )
    assert check_capture(report, mode="authenticated", expect_text="three ninety-one")["passed"] is True
    events.append({"type": "window.rejection", "message": "An actual runtime failure"})
    assert check_capture(report, mode="authenticated")["passed"] is False


def test_positive_metrics_without_pcm_do_not_pass() -> None:
    report = baseline()
    cast("list[dict[str, object]]", report["output"])[0].pop("pcm16")
    assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False


def test_missing_duplicate_reordered_and_miscounted_recorder_blocks_fail() -> None:
    original = baseline()
    original["output"] = [block(), block(4), block(8)]
    cast("list[dict[str, object]]", original["events"])[-1].update(
        {"finalFrame": 12, "samplesCaptured": 12, "blocksCaptured": 3}
    )
    assert check_capture(original, mode="offline", reply_samples=4)["passed"] is True
    for index in (0, 1, 2):
        report = copy.deepcopy(original)
        cast("list[dict[str, object]]", report["output"]).pop(index)
        assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False
    for key, value in (("sequence", 0), ("sampleOffset", 0), ("frame", 128), ("sequence", 1.5)):
        report = copy.deepcopy(original)
        cast("list[dict[str, object]]", report["output"])[1][key] = value
        assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False
    report = copy.deepcopy(original)
    cast("list[dict[str, object]]", report["output"]).reverse()
    assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False
    for key in ("samplesCaptured", "blocksCaptured"):
        report = copy.deepcopy(original)
        cast("list[dict[str, object]]", report["events"])[-1][key] = 0
        assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False


def test_old_capture_without_independent_counters_cannot_pass() -> None:
    report = baseline()
    report["schema"] = "ngn-browser-voice-e2e/v1"
    assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False
    report["schema"] = "ngn-browser-voice-e2e/v2"
    for key in ("sequence", "sampleOffset", "observedFrame"):
        incomplete = copy.deepcopy(report)
        cast("list[dict[str, object]]", incomplete["output"])[0].pop(key)
        assert check_capture(incomplete, mode="offline", reply_samples=4)["passed"] is False


def test_missing_stop_ack_or_missing_partial_tail_fails() -> None:
    report = baseline()
    events = cast("list[dict[str, object]]", report["events"])
    events[-1]["finalFrame"] = 8
    result = check_capture(report, mode="offline", reply_samples=4)
    assert cast("dict[str, bool]", result["checks"])["measurementWindowStopped"] is False
    events.pop()
    assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False


def test_every_pcm_block_is_required_even_after_a_valid_nonzero_block() -> None:
    report = baseline()
    second = block(4)
    report["output"] = [block(), second]
    cast("list[dict[str, object]]", report["events"])[-1].update(
        {"finalFrame": 8, "samplesCaptured": 8, "blocksCaptured": 2}
    )
    assert check_capture(report, mode="offline", reply_samples=4)["passed"] is True
    for invalid in (None, "", "%%%", base64.b64encode(b"\x01\x02").decode(), base64.b64encode(b"\x01").decode()):
        second["pcm16"] = invalid
        result = check_capture(report, mode="offline", reply_samples=4)
        assert cast("dict[str, bool]", result["checks"])["outputPcmComplete"] is False
    second["pcm16"] = block()["pcm16"]
    second["sampleRate"] = float("inf")
    assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False
    second["sampleRate"] = 48000
    second["samples"] = float("nan")
    assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False


def test_connected_label_regression_fails_the_capture_contract() -> None:
    report = baseline()
    events = cast("list[dict[str, object]]", report["events"])
    events.append(
        {"type": "ui.voice", "state": [{"selector": ".voice-controls", "phase": "connected", "text": "Listening"}]}
    )
    assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False


def test_devtools_pretty_fenced_and_enveloped_exports() -> None:
    report = baseline()
    pretty = json.dumps(report, indent=2)
    fenced = f"Script ran on page and returned:\n```json\n{pretty}\n```"
    envelope = json.dumps({"content": [{"type": "text", "text": fenced}]})
    for value in (pretty, fenced, envelope, copy.deepcopy(report)):
        assert extract_report(value) == report


def test_timing_uses_audio_thread_clock_and_excludes_idle_queue_age() -> None:
    report = baseline()
    report["health"] = [
        {
            "contextId": 0,
            "epoch": 0,
            "monotonicMs": 500,
            "audioTime": 0.5,
            "renderedAt": 0.2,
            "outputTimestamp": {"contextTime": 0.5, "performanceTime": 500},
            "playedSamples": 2,
            "bufferedMs": 40,
            "targetMs": 40,
        },
        {"contextId": 0, "epoch": 0, "monotonicMs": 60500, "playedSamples": 4, "bufferedMs": 0},
    ]
    events = cast("list[dict[str, object]]", report["events"])
    events[0]["receivedSamples"] = 2
    events[1]["receivedSamples"] = 4
    result = timing_metrics(report)
    windows = cast("list[dict[str, object]]", result["minuteWindows"])
    assert result["sampleAgeAvailable"] is True
    assert windows[0]["sampleAgeMs"] == {"samples": 1, "p95": 100, "max": 100}
    assert windows[1]["sampleAgeMs"] == {"samples": 0, "p95": 0, "max": 0}
    cast("list[dict[str, object]]", report["health"])[0]["droppedSamples"] = 1
    assert timing_metrics(report)["sampleAgeAvailable"] is False


def test_long_run_contract_rejects_only_early_speech_and_detects_queue_growth() -> None:
    report = baseline()
    # A low-rate synthetic recorder avoids megabytes of PCM in this contract
    # test while retaining exact samples, continuity, and an actual long clock.
    outputs = [
        {**block(index * 4), "sampleRate": 4, "monotonicMs": index * 1000, "contextTimeAtDelivery": index}
        for index in range(306)
    ]
    report["output"] = outputs
    report["input"] = [{**block(), "monotonicMs": 0}, {**block(), "monotonicMs": 300000}]
    events = cast("list[dict[str, object]]", report["events"])
    events[-1].update({"finalFrame": 306 * 4, "samplesCaptured": 306 * 4, "blocksCaptured": 306})
    events.extend(
        [
            {"type": "measurement.start", "monotonicMs": 0},
            {"type": "fixture.start", "round": 1, "monotonicMs": 0, "duration": 1},
            {"type": "fixture.start", "round": 2, "monotonicMs": 300000, "duration": 1},
        ]
    )
    check = partial(
        check_capture, mode="offline", reply_samples=4, min_rounds=2, min_active_span_seconds=300, max_buffered_ms=200
    )
    assert check(report)["passed"] is True
    events[-1]["monotonicMs"] = 10000
    checks = cast("dict[str, bool]", check(report)["checks"])
    assert checks["longActiveSession"] is False
    events[-1]["monotonicMs"] = 300000
    cast("list[dict[str, object]]", report["health"])[0]["bufferedMs"] = 800
    checks = cast("dict[str, bool]", check(report)["checks"])
    assert checks["boundedBrowserPlaybackQueue"] is False
    cast("list[dict[str, object]]", report["health"])[0].update({"bufferedMs": 40, "peakBufferedMs": 800})
    checks = cast("dict[str, bool]", check(report)["checks"])
    assert checks["boundedBrowserPlaybackQueue"] is False


def test_round_requires_rendered_reply_before_next_fixture() -> None:
    report = baseline()
    events = cast("list[dict[str, object]]", report["events"])
    events.extend(
        [
            {"type": "fixture.start", "round": 1, "monotonicMs": 10000, "duration": 1},
            {"type": "fixture.start", "round": 2, "monotonicMs": 20000, "duration": 1},
        ]
    )
    # Existing output is before either utterance. A long wall-clock wait or a
    # successful initial reply cannot stand in for later audible responses.
    checks = cast("dict[str, bool]", check_capture(report, mode="offline", reply_samples=4, min_rounds=2)["checks"])
    assert checks["enoughSpokenRounds"] is False
