"""Artifact validation must distinguish a real interruption from terminal EOF."""

import array
import ast
import base64
import copy
import json
import math
import os
import shutil
import struct
import subprocess
import wave
from collections.abc import Callable
from functools import partial
from itertools import pairwise
from pathlib import Path
from typing import cast

import pytest
from examples.live.browser_e2e.artifacts import _normalized_pcm_correlation
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


def add_measurement_checkpoint(report: dict[str, object], rendered_end: int) -> None:
    events = cast("list[dict[str, object]]", report["events"])
    trace = next((event for event in events if event.get("type") in {"playback.trace", "playback.rendered"}), {})
    context_id = trace.get("contextId", 0)
    epoch = trace.get("epoch", 0)
    source_start = trace.get("sourceStart", 0)
    starts = [event for event in events if event.get("type") == "measurement.start"]
    if not starts:
        start = {
            "type": "measurement.start",
            "contextId": context_id,
            "epoch": epoch,
            "sourceStart": source_start,
            "beginRequestId": 6,
            "beginAcceptedEnd": source_start,
            "transportReceivedSamplesAtStart": source_start,
            "monotonicMs": 1000,
        }
        events.append(start)
    else:
        start = starts[0]
        start.update(
            {
                "contextId": context_id,
                "epoch": epoch,
                "sourceStart": source_start,
                "beginRequestId": 6,
                "beginAcceptedEnd": source_start,
                "transportReceivedSamplesAtStart": source_start,
            }
        )
    events.extend(
        [
            {
                "type": "measurement.checkpoint-request",
                "phase": "begin",
                "requestId": 6,
                "contextId": context_id,
                "epoch": epoch,
                "submittedEnd": source_start,
                "receivedSamples": 0,
                "transportReceivedSamples": source_start,
                "captureOrder": 99_999,
                "monotonicMs": 900,
            },
            {
                "type": "playback.trace-checkpoint",
                "requestId": 6,
                "contextId": context_id,
                "epoch": epoch,
                "status": "completed",
                "acceptedEnd": source_start,
                "receivedEnd": source_start,
                "renderedEnd": source_start,
                "renderedAt": 2.0,
                "omittedIntervals": 0,
            },
            {
                "type": "measurement.checkpoint-request",
                "requestId": 7,
                "contextId": context_id,
                "epoch": epoch,
                "phase": "end",
                "submittedEnd": rendered_end,
                "receivedSamples": rendered_end - cast("int", source_start),
                "transportReceivedSamples": rendered_end,
                "captureOrder": 100_000,
                "monotonicMs": 2000,
            },
            {
                "type": "playback.trace-checkpoint",
                "requestId": 7,
                "contextId": context_id,
                "epoch": epoch,
                "status": "completed",
                "acceptedEnd": rendered_end,
                "receivedEnd": rendered_end,
                "renderedEnd": rendered_end,
                "renderedAt": 2.02,
                "omittedIntervals": 0,
            },
            {
                "type": "measurement.end",
                "requestId": 7,
                "contextId": context_id,
                "epoch": epoch,
                "acceptedEnd": rendered_end,
                "renderedEnd": rendered_end,
                "transportReceivedSamples": rendered_end,
                "monotonicMs": 2020,
            },
        ]
    )


def rendered_speech_report(*, audible: bool = True, source_start: int = 0) -> dict[str, object]:
    pcm = struct.pack("<4h", 0, 1000, -1200, 0)
    output = struct.pack("<8h", *([1000] * 8)) if audible else struct.pack("<8h", *([0] * 8))
    report = {
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
    add_measurement_checkpoint(report, source_start + 4)
    return report


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
            "sampleRate": 24000,
            "contextSampleRate": 48000,
            "at": 1 + 4 / 24000,
            "until": 1 + 7 / 24000,
            "contextTimeAtDelivery": 1 + 4 / 24000,
            "outputTimestamp": {"contextTime": 1 + 4 / 24000, "performanceTime": 500},
            "monotonicMs": 500,
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
    second = next(event for event in events if event.get("type") == "playback.rendered").copy()
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


@pytest.mark.parametrize(
    ("field", "value"),
    [("sampleRate", 48000), ("at", -0.01), ("until", 2.0)],
)
def test_rendered_speech_age_rejects_wrong_pcm_rate_and_invalid_time_span(field: str, value: object) -> None:
    report = rendered_speech_report()
    rendered = cast("list[dict[str, object]]", report["events"])[1]
    rendered[field] = value
    result = cast("dict[str, object]", timing_metrics(report)["renderedSpeechAge"])
    assert result["available"] is False
    assert result["invalidBlocks"] == 1
    assert result["withinBounds"] is False


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
    packet = struct.pack("<4h", 0, 1000, -1200, 0)
    report["received"] = [
        {
            "sequence": 0,
            "transportSequence": 0,
            "captureOrder": 1,
            "sampleOffset": 0,
            "receivedSamples": 4,
            "transportReceivedSamples": 4,
            "samples": 4,
            "bytes": len(packet),
            "sampleRate": 24000,
            "contextId": 0,
            "epoch": 2,
            "monotonicMs": speech_received_ms,
            "pcm16": base64.b64encode(packet).decode(),
        }
    ]
    report["receivedPcm"] = {
        "sampleFormat": "s16le",
        "sampleRate": 24000,
        "packetCount": 1,
        "capturedPackets": 1,
        "samples": 4,
        "capturedSamples": 4,
        "capturedBytes": 8,
        "omittedPackets": 0,
        "omittedBytes": 0,
        "byteLimit": 30_000_000,
    }
    report["output"] = [
        {
            "contextId": 0,
            "frame": 48004,
            "observedFrame": 48004,
            "sampleOffset": 0,
            "sequence": 0,
            "samples": 8,
            "sampleRate": 48000,
            "monotonicMs": 6000,
            "contextTimeAtDelivery": 1 + 4 / 48000,
            "outputTimestamp": {"contextTime": 1 + 4 / 48000, "performanceTime": 3000},
            "pcm16": base64.b64encode(struct.pack("<8h", 0, 0, 1000, 1000, -1200, -1200, 0, 0)).decode(),
        },
        {
            "contextId": 0,
            "frame": 48012,
            "observedFrame": 48012,
            "sampleOffset": 8,
            "sequence": 1,
            "samples": 4,
            "sampleRate": 48000,
            "monotonicMs": 6400,
            "contextTimeAtDelivery": 1 + 12 / 48000,
            "outputTimestamp": {"contextTime": 1 + 12 / 48000, "performanceTime": 6400},
            "pcm16": base64.b64encode(struct.pack("<4h", 1000, 1000, 1000, 1000)).decode(),
        },
    ]
    events = cast("list[dict[str, object]]", report["events"])
    for event in events:
        if event.get("type") == "recorder.capture-start":
            event.update({"contextId": 0, "frame": 48004, "sampleOffset": 0, "sequence": 0})
        elif event.get("type") == "recorder.capture-stop":
            event.update({"contextId": 0, "finalFrame": 48016, "samplesCaptured": 12, "blocksCaptured": 2})
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
                "receivedSequence": 0,
                "receivedSamples": 4,
                "receiveMonotonicMs": speech_received_ms,
            },
            {
                "type": "playback.trace",
                "contextId": 0,
                "epoch": 2,
                "sourceStart": 0,
                "samples": 4,
                "sequence": 0,
                "batchIntervals": 1,
                "omittedIntervals": 0,
                "at": 48004 / 48000,
                "until": 48004 / 48000 + 3 / 24000,
                "contextTimeAtDelivery": 48004 / 48000,
                "captureOrder": 100,
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
    add_measurement_checkpoint(report, 4)
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


def traced_graph_report(*, delayed_gap: bool = False, output_silent: bool = False) -> dict[str, object]:
    source = [((index * 73) % 1801) - 900 for index in range(480)]
    spans = [(0, 480, 2.0)] if not delayed_gap else [(0, 240, 2.0), (240, 240, 2.2)]
    delivery_context_time = max(at + (count - 1) / 24000 for _, count, at in spans) + 0.01
    arrival_time = 1100 + (spans[0][2] - delivery_context_time) * 1000 - 100
    output = [0] * 10_100
    for source_start, count, at in spans:
        if output_silent:
            continue
        target = round(at * 48000) - 96_000
        for offset in range(count):
            output[target + offset * 2] = source[source_start + offset]
            output[target + offset * 2 + 1] = source[source_start + offset]
    packet_bytes = struct.pack("<480h", *source)
    output_bytes = struct.pack(f"<{len(output)}h", *output)
    report = authenticated_scenario_capture()
    report["contexts"] = [{"id": 0, "sampleRate": 48000}]
    report["received"] = [
        {
            "sequence": 0,
            "transportSequence": 0,
            "captureOrder": 1,
            "sampleOffset": 0,
            "receivedSamples": 480,
            "transportReceivedSamples": 480,
            "samples": 480,
            "bytes": len(packet_bytes),
            "sampleRate": 24000,
            "contextId": 0,
            "epoch": 0,
            "monotonicMs": arrival_time,
            "pcm16": base64.b64encode(packet_bytes).decode(),
        }
    ]
    report["receivedPcm"] = {
        "sampleFormat": "s16le",
        "sampleRate": 24000,
        "packetCount": 1,
        "capturedPackets": 1,
        "samples": 480,
        "capturedSamples": 480,
        "capturedBytes": len(packet_bytes),
        "omittedPackets": 0,
        "omittedBytes": 0,
        "byteLimit": 30_000_000,
    }
    events = cast("list[dict[str, object]]", report["events"])
    events.extend(
        [
            {
                "type": "playback.submit",
                "contextId": 0,
                "epoch": 0,
                "samples": 480,
                "bytes": 960,
                "submittedSamples": 480,
                "receivedSequence": 0,
                "receivedSamples": 480,
                "receiveMonotonicMs": arrival_time,
            },
        ]
    )
    for index, (source_start, count, at) in enumerate(spans):
        events.append(
            {
                "type": "playback.trace",
                "contextId": 0,
                "epoch": 0,
                "sequence": 0,
                "batchIntervals": len(spans),
                "omittedIntervals": 0,
                "sourceStart": source_start,
                "samples": count,
                "at": at,
                "until": at + (count - 1) / 24000,
                "contextTimeAtDelivery": delivery_context_time,
                "monotonicMs": 1100,
                "captureOrder": index + 100,
            }
        )
    report["output"] = [
        {
            "contextId": 0,
            "frame": 96_000,
            "observedFrame": 96_000,
            "sampleOffset": 0,
            "sequence": 0,
            "samples": len(output),
            "sampleRate": 48000,
            "monotonicMs": 1100,
            "contextTimeAtDelivery": 2.0,
            "pcm16": base64.b64encode(output_bytes).decode(),
        }
    ]
    report["health"] = [
        {
            "type": "health",
            "contextId": 0,
            "epoch": 0,
            "receivedSamples": 480,
            "playedSamples": 480,
            "droppedSamples": 0,
            "interruptedSamples": 0,
            "underruns": 0,
        }
    ]
    add_measurement_checkpoint(report, 480)
    return report


def fractional_phase_report(*, isolated_transient: bool = False, unrelated_output: bool = False) -> dict[str, object]:
    state = 0x12345678
    source = []
    for _ in range(480):
        state = (1664525 * state + 1013904223) & 0xFFFFFFFF
        source.append(((state >> 8) & 0xFFFF) - 32768)
    if isolated_transient:
        source = [0] * 480
        source[240] = 12000
    report = traced_graph_report()
    packet_bytes = struct.pack("<480h", *source)
    cast("list[dict[str, object]]", report["received"])[0]["pcm16"] = base64.b64encode(packet_bytes).decode()
    source_output = source
    if unrelated_output:
        state = 0x9ABCDEF0
        source_output = []
        for _ in range(480):
            state = (1664525 * state + 1013904223) & 0xFFFFFFFF
            source_output.append(((state >> 8) & 0xFFFF) - 32768)
    output = [0] * 10_100
    phase_frames = 1.8
    for frame in range(2 * len(source) + 6):
        source_position = (frame - phase_frames) / 2
        left = math.floor(source_position)
        phase = source_position - left
        left_value = source_output[left] if 0 <= left < len(source_output) else 0
        right_value = source_output[left + 1] if 0 <= left + 1 < len(source_output) else 0
        output[frame] = round(left_value * (1 - phase) + right_value * phase)
    cast("list[dict[str, object]]", report["output"])[0]["pcm16"] = base64.b64encode(
        struct.pack(f"<{len(output)}h", *output)
    ).decode()
    return report


def test_graph_render_age_uses_trace_and_correlates_attenuated_output_pcm() -> None:
    report = traced_graph_report()
    result = cast("dict[str, object]", timing_metrics(report)["graphRenderedSpeechAge"])
    assert result["available"] is True
    assert result["clock"] == "software_output_graph"
    assert cast("int", result["observations"]) > 0
    assert result["p95Ms"] == pytest.approx(119, abs=1)
    assert result["withinBounds"] is True

    # The graph check is scale-independent; real app volume attenuation must
    # not turn present matching PCM into an unconfirmed interval.
    quiet = traced_graph_report()
    quiet_output = cast("list[dict[str, object]]", quiet["output"])[0]
    samples = array.array("h", struct.unpack("<10100h", base64.b64decode(cast("str", quiet_output["pcm16"]))))
    for index, value in enumerate(samples):
        samples[index] = round(value * 0.04)
    quiet_output["pcm16"] = base64.b64encode(samples.tobytes()).decode()
    attenuated = cast("dict[str, object]", timing_metrics(quiet)["graphRenderedSpeechAge"])
    assert attenuated["available"] is True


def test_graph_checkpoint_excludes_preloaded_unaligned_warmup_from_measured_prefix() -> None:
    report = traced_graph_report()
    received = cast("list[dict[str, object]]", report["received"])
    warmup_bytes = struct.pack("<7h", *([0] * 7))
    warmup = {
        **received[0],
        "sequence": 0,
        "transportSequence": 0,
        "captureOrder": 50,
        "sampleOffset": 0,
        "receivedSamples": 7,
        "transportReceivedSamples": 7,
        "samples": 7,
        "bytes": len(warmup_bytes),
        "measurementSampleOffset": None,
        "measurementReceivedSamples": 0,
        "pcm16": base64.b64encode(warmup_bytes).decode(),
    }
    voice = received[0]
    voice.update(
        {
            "sequence": 1,
            "transportSequence": 1,
            "captureOrder": 51,
            "sampleOffset": 7,
            "receivedSamples": 487,
            "transportReceivedSamples": 487,
        }
    )
    received.insert(0, warmup)
    cast("dict[str, object]", report["receivedPcm"]).update(
        packetCount=2, capturedPackets=2, samples=487, capturedSamples=487, capturedBytes=974
    )
    events = cast("list[dict[str, object]]", report["events"])
    start = next(event for event in events if event.get("type") == "measurement.start")
    start.update(
        {
            "sourceStart": 7,
            "beginAcceptedEnd": 7,
            "transportReceivedSamplesAtStart": 7,
            "transportReceivedPacketsAtStart": 1,
        }
    )
    begin_request = next(
        event
        for event in events
        if event.get("type") == "measurement.checkpoint-request" and event.get("phase") == "begin"
    )
    begin_request.update({"submittedEnd": 7, "transportReceivedSamples": 7})
    begin_response = next(
        event for event in events if event.get("type") == "playback.trace-checkpoint" and event.get("requestId") == 6
    )
    begin_response.update({"acceptedEnd": 7, "receivedEnd": 7, "renderedEnd": 7})
    end_request = next(
        event
        for event in events
        if event.get("type") == "measurement.checkpoint-request" and event.get("phase") == "end"
    )
    end_request.update({"submittedEnd": 487, "receivedSamples": 480, "transportReceivedSamples": 487})
    end_response = next(
        event for event in events if event.get("type") == "playback.trace-checkpoint" and event.get("requestId") == 7
    )
    end_response.update({"acceptedEnd": 487, "receivedEnd": 487, "renderedEnd": 487})
    next(event for event in events if event.get("type") == "measurement.end").update(
        {"acceptedEnd": 487, "renderedEnd": 487, "transportReceivedSamples": 487}
    )
    voice_submit = next(event for event in events if event.get("type") == "playback.submit")
    voice_submit.update({"submittedSamples": 487, "receivedSequence": 1, "receivedSamples": 487})
    events.insert(
        events.index(voice_submit),
        {
            "type": "playback.submit",
            "contextId": 0,
            "epoch": 0,
            "samples": 7,
            "bytes": 14,
            "submittedSamples": 7,
            "receivedSequence": 0,
            "receiveMonotonicMs": 900,
        },
    )
    voiced_trace = next(event for event in events if event.get("type") == "playback.trace")
    voiced_trace.update({"sourceStart": 7, "sequence": 1, "captureOrder": 101})
    events.insert(
        events.index(voiced_trace),
        {
            **voiced_trace,
            "sourceStart": 0,
            "samples": 7,
            "sequence": 0,
            "batchIntervals": 1,
            "at": 1.0,
            "until": 1 + 6 / 24000,
            "captureOrder": 100,
        },
    )

    metric = cast("dict[str, object]", timing_metrics(report)["graphRenderedSpeechAge"])
    assert metric["available"] is True
    assert metric["checkpointSourceStart"] == 7
    assert metric["checkpointRenderedEnd"] == 487
    assert metric["unsubmittedReceivedPackets"] == 0


def test_graph_checkpoint_rejects_an_interval_that_crosses_the_start_boundary() -> None:
    report = traced_graph_report()
    events = cast("list[dict[str, object]]", report["events"])
    start = next(event for event in events if event.get("type") == "measurement.start")
    start.update({"sourceStart": 1, "beginAcceptedEnd": 1, "transportReceivedSamplesAtStart": 1})
    begin_request = next(
        event
        for event in events
        if event.get("type") == "measurement.checkpoint-request" and event.get("phase") == "begin"
    )
    begin_request.update({"submittedEnd": 1, "transportReceivedSamples": 1})
    begin_response = next(
        event for event in events if event.get("type") == "playback.trace-checkpoint" and event.get("requestId") == 6
    )
    begin_response.update({"acceptedEnd": 1, "receivedEnd": 1, "renderedEnd": 1})

    metric = cast("dict[str, object]", timing_metrics(report)["graphRenderedSpeechAge"])
    assert metric["available"] is False
    assert metric["checkpointBoundaryCoordinateErrors"] == 1


def test_graph_render_correlation_handles_fractional_phase_and_quiet_context() -> None:
    report = fractional_phase_report()
    source = array.array(
        "h",
        struct.unpack(
            "<480h", base64.b64decode(cast("str", cast("list[dict[str, object]]", report["received"])[0]["pcm16"]))
        ),
    )
    output = array.array(
        "h",
        struct.unpack(
            "<10100h", base64.b64decode(cast("str", cast("list[dict[str, object]]", report["output"])[0]["pcm16"]))
        ),
    )
    interval = next(
        event for event in cast("list[dict[str, object]]", report["events"]) if event.get("type") == "playback.trace"
    )
    offsets = list(range(480))
    coarse = max(
        _normalized_pcm_correlation(source, output, offsets, 2, 2 + 479 / 24000, 480, 48000, 96_000, shift)
        for shift in range(-128, 129, 8)
    )
    fine = max(
        _normalized_pcm_correlation(
            source,
            output,
            offsets,
            cast("float", interval["at"]),
            cast("float", interval["until"]),
            480,
            48000,
            96_000,
            shift,
        )
        for shift in range(-2, 3)
    )
    assert coarse < 0.65
    assert fine > 0.9
    result = cast("dict[str, object]", timing_metrics(report)["graphRenderedSpeechAge"])
    assert result["available"] is True

    transient = cast(
        "dict[str, object]",
        timing_metrics(fractional_phase_report(isolated_transient=True))["graphRenderedSpeechAge"],
    )
    assert transient["available"] is True
    unrelated = cast(
        "dict[str, object]",
        timing_metrics(fractional_phase_report(unrelated_output=True))["graphRenderedSpeechAge"],
    )
    assert unrelated["available"] is False
    assert unrelated["uncorrelatedOutputIntervals"] == 1


def test_graph_render_age_rejects_missing_trace_tail_and_silent_output() -> None:
    report = traced_graph_report()
    events = cast("list[dict[str, object]]", report["events"])
    events[0:0] = []
    events[:] = [event for event in events if event.get("type") != "playback.trace"]
    missing = cast("dict[str, object]", timing_metrics(report)["graphRenderedSpeechAge"])
    assert missing["available"] is False
    assert missing["observations"] == 0

    partial = traced_graph_report()
    cast("list[dict[str, object]]", partial["health"])[0]["playedSamples"] = 960
    tail = cast("dict[str, object]", timing_metrics(partial)["graphRenderedSpeechAge"])
    assert tail["available"] is True  # Atomic checkpoint is authoritative; final health can be stale.
    assert tail["checkpointValid"] is True

    missing_checkpoint = traced_graph_report()
    cast("list[dict[str, object]]", missing_checkpoint["events"]).__delitem__(slice(None))
    cast("list[dict[str, object]]", missing_checkpoint["events"]).extend(
        event
        for event in cast("list[dict[str, object]]", traced_graph_report()["events"])
        if event.get("type")
        not in {"measurement.start", "measurement.checkpoint-request", "playback.trace-checkpoint", "measurement.end"}
    )
    missing = cast("dict[str, object]", timing_metrics(missing_checkpoint)["graphRenderedSpeechAge"])
    assert missing["available"] is False
    assert missing["checkpointValid"] is False

    interrupted = traced_graph_report()
    checkpoint = next(
        event
        for event in cast("list[dict[str, object]]", interrupted["events"])
        if event.get("type") == "playback.trace-checkpoint"
    )
    checkpoint["status"] = "interrupted"
    invalid = cast("dict[str, object]", timing_metrics(interrupted)["graphRenderedSpeechAge"])
    assert invalid["available"] is False

    wrong_id = traced_graph_report()
    end_request = next(
        event
        for event in cast("list[dict[str, object]]", wrong_id["events"])
        if event.get("type") == "measurement.checkpoint-request" and event.get("phase") == "end"
    )
    end_request["requestId"] = 999
    assert cast("dict[str, object]", timing_metrics(wrong_id)["graphRenderedSpeechAge"])["available"] is False

    overshoot = traced_graph_report()
    end_checkpoint = next(
        event
        for event in cast("list[dict[str, object]]", overshoot["events"])
        if event.get("type") == "playback.trace-checkpoint" and event.get("requestId") == 7
    )
    end_checkpoint["renderedEnd"] = 700
    assert cast("dict[str, object]", timing_metrics(overshoot)["graphRenderedSpeechAge"])["available"] is False
    assert tail["tailCoordinateErrors"] == 0

    omitted = traced_graph_report()
    next(
        event for event in cast("list[dict[str, object]]", omitted["events"]) if event.get("type") == "playback.trace"
    )["omittedIntervals"] = 1
    missing_interval = cast("dict[str, object]", timing_metrics(omitted)["graphRenderedSpeechAge"])
    assert missing_interval["available"] is False
    assert missing_interval["omittedIntervals"] == 1

    silent = cast(
        "dict[str, object]", timing_metrics(traced_graph_report(output_silent=True))["graphRenderedSpeechAge"]
    )
    assert silent["available"] is False
    assert silent["uncorrelatedOutputIntervals"] == 1


def test_post_checkpoint_pcm_is_retained_and_reported_outside_the_verified_prefix() -> None:
    report = traced_graph_report()
    packet_bytes = struct.pack("<480h", *([0] * 480))
    cast("list[dict[str, object]]", report["received"]).append(
        {
            "sequence": 1,
            "transportSequence": 1,
            "captureOrder": 100_001,
            "sampleOffset": 480,
            "receivedSamples": 960,
            "transportReceivedSamples": 960,
            "samples": 480,
            "bytes": 960,
            "sampleRate": 24000,
            "contextId": 0,
            "epoch": 0,
            "monotonicMs": 2021,
            "pcm16": base64.b64encode(packet_bytes).decode(),
        }
    )
    cast("dict[str, object]", report["receivedPcm"]).update(
        packetCount=2, capturedPackets=2, samples=960, capturedSamples=960, capturedBytes=1920
    )
    events = cast("list[dict[str, object]]", report["events"])
    trace = next(event for event in events if event.get("type") == "playback.trace")
    events.append(
        {**trace, "sequence": 1, "sourceStart": 480, "at": 2.02, "until": 2.02 + 479 / 24000, "captureOrder": 100_001}
    )

    metric = cast("dict[str, object]", timing_metrics(report)["graphRenderedSpeechAge"])
    assert metric["available"] is True
    assert metric["postCheckpointReceivedSamples"] == 480
    assert metric["traceIntervalsOutsideMeasuredPrefix"] == 1
    assert metric["unsubmittedReceivedPackets"] == 0


def test_graph_render_age_requires_complete_received_ledger_including_quiet_packets() -> None:
    missing_quiet_packet = traced_graph_report()
    object_record(missing_quiet_packet, "receivedPcm").update(
        packetCount=2, capturedPackets=2, samples=960, capturedSamples=960, capturedBytes=1920
    )
    incomplete = cast("dict[str, object]", timing_metrics(missing_quiet_packet)["graphRenderedSpeechAge"])
    assert incomplete["available"] is False
    assert incomplete["receivedPcmComplete"] is False

    duplicate = traced_graph_report()
    packets = cast("list[dict[str, object]]", duplicate["received"])
    duplicate_packet = packets[0].copy()
    duplicate_packet.update(
        sequence=1,
        sampleOffset=480,
        receivedSamples=960,
        transportReceivedSamples=960,
        transportSequence=0,
        pcm16=base64.b64encode(bytes(960)).decode(),
    )
    packets.append(duplicate_packet)
    object_record(duplicate, "receivedPcm").update(
        packetCount=2, capturedPackets=2, samples=960, capturedSamples=960, capturedBytes=1920
    )
    repeated = cast("dict[str, object]", timing_metrics(duplicate)["graphRenderedSpeechAge"])
    assert repeated["available"] is False
    assert repeated["duplicateReceivedSequences"] == 1

    lying_summary = traced_graph_report()
    object_record(lying_summary, "receivedPcm")["capturedBytes"] = 962
    invalid = cast("dict[str, object]", timing_metrics(lying_summary)["graphRenderedSpeechAge"])
    assert invalid["available"] is False
    assert invalid["receivedPcmComplete"] is False


def test_graph_render_age_fails_when_source_voice_gap_is_extended() -> None:
    result = cast("dict[str, object]", timing_metrics(traced_graph_report(delayed_gap=True))["graphRenderedSpeechAge"])
    assert result["available"] is True
    assert cast("float", result["maximumAddedVoicedGapMs"]) > 40
    assert result["voicedGapOver40Ms"] is True
    assert result["withinBounds"] is False


def test_configured_rendered_speech_limit_is_independent_of_default_report_bounds() -> None:
    report = traced_graph_report()
    events = cast("list[dict[str, object]]", report["events"])
    packet = cast("list[dict[str, object]]", report["received"])[0]
    packet["monotonicMs"] = cast("float", packet["monotonicMs"]) - 140
    submit = next(event for event in events if event.get("type") == "playback.submit")
    submit["receiveMonotonicMs"] = cast("float", submit["receiveMonotonicMs"]) - 140
    metric = cast("dict[str, object]", timing_metrics(report)["graphRenderedSpeechAge"])
    assert metric["voiceContinuityVerified"] is True
    assert cast("float", metric["p95Ms"]) > 250
    assert metric["withinBounds"] is False

    custom = check_authenticated_scenario(
        report,
        [],
        require_rendered_speech_telemetry=True,
        max_rendered_speech_p95_ms=300,
        max_rendered_speech_peak_ms=1000,
    )
    custom_checks = cast("dict[str, bool]", custom["checks"])
    assert custom_checks["renderedSpeechGapWithinBound"] is True
    assert custom_checks["renderedSpeechP95WithinBound"] is True

    default = check_authenticated_scenario(
        report,
        [],
        require_rendered_speech_telemetry=True,
        max_rendered_speech_p95_ms=250,
        max_rendered_speech_peak_ms=1000,
    )
    default_checks = cast("dict[str, bool]", default["checks"])
    assert default_checks["renderedSpeechGapWithinBound"] is True
    assert default_checks["renderedSpeechP95WithinBound"] is False


def traced_pause_report(*, pause_samples: int, extra_ms: float) -> dict[str, object]:
    first_voice = [((index * 73) % 1801) - 900 for index in range(480)]
    second_voice = [((index * 101) % 1701) - 850 for index in range(480)]
    source = first_voice + [0] * pause_samples + second_voice
    pause_end = 480 + pause_samples
    boundaries = [0, 480]
    cursor = 480
    while cursor < pause_end:
        cursor = min(pause_end, cursor + 480)
        boundaries.append(cursor)
    boundaries.append(len(source))
    intervals: list[tuple[int, int, float]] = []
    for low, high in pairwise(boundaries):
        at = 2 + low / 24000 + (extra_ms / 1000 if low >= pause_end else 0)
        intervals.append((low, high - low, at))
    origin = 96_000
    final_time = intervals[-1][2] + (intervals[-1][1] - 1) / 24000
    delivery_context_time = final_time + 0.01
    delivery_monotonic_ms = 1100 + (delivery_context_time - 2) * 1000
    output = [0] * (round((final_time - 2) * 48000) + 4)
    for low, count, at in intervals:
        if low >= 480 and low < pause_end:
            continue
        for offset in range(count):
            target = round((at + offset / 24000) * 48000) - origin
            value = source[low + offset]
            output[target] = output[target + 1] = value
    source_bytes = struct.pack(f"<{len(source)}h", *source)
    report = authenticated_scenario_capture()
    report["contexts"] = [{"id": 0, "sampleRate": 48000}]
    report["received"] = [
        {
            "sequence": 0,
            "transportSequence": 0,
            "captureOrder": 1,
            "sampleOffset": 0,
            "receivedSamples": len(source),
            "transportReceivedSamples": len(source),
            "samples": len(source),
            "bytes": len(source_bytes),
            "sampleRate": 24000,
            "contextId": 0,
            "epoch": 0,
            "monotonicMs": 1000,
            "pcm16": base64.b64encode(source_bytes).decode(),
        }
    ]
    report["receivedPcm"] = {
        "sampleFormat": "s16le",
        "sampleRate": 24000,
        "packetCount": 1,
        "capturedPackets": 1,
        "samples": len(source),
        "capturedSamples": len(source),
        "capturedBytes": len(source_bytes),
        "omittedPackets": 0,
        "omittedBytes": 0,
        "byteLimit": 30_000_000,
    }
    events = cast("list[dict[str, object]]", report["events"])
    events.extend(
        [
            {
                "type": "playback.submit",
                "contextId": 0,
                "epoch": 0,
                "samples": len(source),
                "bytes": len(source_bytes),
                "submittedSamples": len(source),
                "receivedSequence": 0,
                "receivedSamples": len(source),
                "receiveMonotonicMs": 1000,
            }
        ]
    )
    for index, (low, count, at) in enumerate(intervals):
        sequence = index // 10
        sequence_start = sequence * 10
        batch_count = min(10, len(intervals) - sequence_start)
        events.append(
            {
                "type": "playback.trace",
                "contextId": 0,
                "epoch": 0,
                "sequence": sequence,
                "batchIntervals": batch_count,
                "omittedIntervals": 0,
                "sourceStart": low,
                "samples": count,
                "at": at,
                "until": at + (count - 1) / 24000,
                "contextTimeAtDelivery": delivery_context_time,
                "monotonicMs": delivery_monotonic_ms,
                "captureOrder": 100 + index,
            }
        )
    output_bytes = struct.pack(f"<{len(output)}h", *output)
    report["output"] = [
        {
            "contextId": 0,
            "frame": origin,
            "observedFrame": origin,
            "sampleOffset": 0,
            "sequence": 0,
            "samples": len(output),
            "sampleRate": 48000,
            "monotonicMs": delivery_monotonic_ms,
            "contextTimeAtDelivery": delivery_context_time,
            "pcm16": base64.b64encode(output_bytes).decode(),
        }
    ]
    report["health"] = [
        {
            "contextId": 0,
            "epoch": 0,
            "playedSamples": len(source),
            "droppedSamples": 0,
            "interruptedSamples": 0,
            "underruns": 1,
        }
    ]
    add_measurement_checkpoint(report, len(source))
    return report


def test_short_source_pause_gates_only_excess_render_delay_and_long_pause_is_reported() -> None:
    # 61 ms of silence inside one source stream rendered as 78 ms is 17 ms
    # excess; a 209 ms rendering of that same source pause adds 148 ms.
    source_pause = traced_pause_report(pause_samples=1464, extra_ms=17)
    candidate = cast("dict[str, object]", timing_metrics(source_pause)["graphRenderedSpeechAge"])
    assert candidate["available"] is True
    assert cast("float", candidate["maximumClassifiableAddedGapMs"]) < 40
    assert candidate["voicedGapOver40Ms"] is False

    delayed = cast(
        "dict[str, object]",
        timing_metrics(traced_pause_report(pause_samples=1464, extra_ms=148))["graphRenderedSpeechAge"],
    )
    assert cast("float", delayed["maximumClassifiableAddedGapMs"]) > 40
    assert delayed["voicedGapOver40Ms"] is True
    delayed_gate = check_authenticated_scenario(
        traced_pause_report(pause_samples=1464, extra_ms=148),
        [],
        require_rendered_speech_telemetry=True,
        max_rendered_speech_p95_ms=250,
        max_rendered_speech_peak_ms=1000,
    )
    assert cast("dict[str, bool]", delayed_gate["checks"])["renderedSpeechGapWithinBound"] is False

    long_pause = cast(
        "dict[str, object]",
        timing_metrics(traced_pause_report(pause_samples=6000, extra_ms=300))["graphRenderedSpeechAge"],
    )
    assert cast("int", long_pause["longSourcePauseGaps"]) > 0
    assert cast("float", long_pause["maximumAddedVoicedGapMs"]) > 40
    assert long_pause["voicedGapOver40Ms"] is False


def with_near_voice_unclassified_underrun(report: dict[str, object]) -> dict[str, object]:
    sample_count = cast("int", object_record(report, "receivedPcm")["samples"])
    report["health"] = [
        {
            "contextId": 0,
            "epoch": 0,
            "monotonicMs": 1000,
            "receivedSamples": sample_count,
            "playedSamples": 0,
            "droppedSamples": 0,
            "interruptedSamples": 0,
            "underruns": 0,
            "bufferedMs": 20,
            "lastUnderrunAt": -1,
        },
        {
            "contextId": 0,
            "epoch": 0,
            "monotonicMs": 1050,
            "receivedSamples": sample_count + 10,
            "playedSamples": sample_count,
            "droppedSamples": 0,
            "interruptedSamples": 0,
            "underruns": 1,
            "bufferedMs": 1,
            "lastUnderrunAt": -1,
        },
    ]
    return report


def test_complete_graph_trace_overrides_only_counter_only_starvation_classification() -> None:
    result = check_authenticated_scenario(
        with_near_voice_unclassified_underrun(traced_pause_report(pause_samples=1464, extra_ms=17)),
        [],
        require_rendered_speech_telemetry=True,
        max_rendered_speech_p95_ms=250,
        max_rendered_speech_peak_ms=1000,
    )
    checks = cast("dict[str, bool]", result["checks"])
    details = cast("dict[str, object]", result["details"])
    assert len(cast("list[dict[str, object]]", details["unclassifiedUnderruns"])) == 1
    assert details["counterClassificationsOverriddenByCompleteGraphTrace"] is True
    assert checks["noUnclassifiedQueuedUnderruns"] is True
    assert checks["renderedSpeechGapWithinBound"] is True

    delayed = check_authenticated_scenario(
        with_near_voice_unclassified_underrun(traced_pause_report(pause_samples=1464, extra_ms=148)),
        [],
        require_rendered_speech_telemetry=True,
        max_rendered_speech_p95_ms=250,
        max_rendered_speech_peak_ms=1000,
    )
    delayed_checks = cast("dict[str, bool]", delayed["checks"])
    assert delayed_checks["noUnclassifiedQueuedUnderruns"] is False
    assert delayed_checks["renderedSpeechGapWithinBound"] is False

    no_trace = with_near_voice_unclassified_underrun(traced_pause_report(pause_samples=1464, extra_ms=17))
    no_trace_events = cast("list[dict[str, object]]", no_trace["events"])
    no_trace_events[:] = [event for event in no_trace_events if event.get("type") != "playback.trace"]
    missing_trace = check_authenticated_scenario(
        no_trace,
        [],
        require_rendered_speech_telemetry=True,
        max_rendered_speech_p95_ms=250,
        max_rendered_speech_peak_ms=1000,
    )
    missing_checks = cast("dict[str, bool]", missing_trace["checks"])
    assert missing_checks["noUnclassifiedQueuedUnderruns"] is False
    assert missing_checks["renderedSpeechTelemetryAvailable"] is False


def test_long_packet_gap_without_source_silence_is_not_claimed_as_verified_smoothness() -> None:
    report = traced_graph_report(delayed_gap=True)
    source_values = [((index * 73) % 1801) - 900 for index in range(480)]
    chunks = [source_values[:240], source_values[240:]]
    packets: list[dict[str, object]] = []
    for index, chunk in enumerate(chunks):
        raw = struct.pack("<240h", *chunk)
        packets.append(
            {
                "sequence": index,
                "transportSequence": index,
                "captureOrder": index + 1,
                "sampleOffset": index * 240,
                "receivedSamples": (index + 1) * 240,
                "transportReceivedSamples": (index + 1) * 240,
                "samples": 240,
                "bytes": len(raw),
                "sampleRate": 24000,
                "contextId": 0,
                "epoch": 0,
                "monotonicMs": 1000 + index * 301,
                "pcm16": base64.b64encode(raw).decode(),
            }
        )
    report["received"] = packets
    object_record(report, "receivedPcm").update(
        packetCount=2,
        capturedPackets=2,
        samples=480,
        capturedSamples=480,
        capturedBytes=960,
        omittedPackets=0,
        omittedBytes=0,
    )
    events = cast("list[dict[str, object]]", report["events"])
    events[:] = [event for event in events if event.get("type") != "playback.submit"]
    for event in events:
        if event.get("type") == "playback.trace":
            event["monotonicMs"] = 1400
    events.extend(
        [
            {
                "type": "playback.submit",
                "contextId": 0,
                "epoch": 0,
                "samples": 240,
                "bytes": 480,
                "submittedSamples": 240,
                "receivedSequence": 0,
                "receivedSamples": 240,
                "receiveMonotonicMs": 1000,
            },
            {
                "type": "playback.submit",
                "contextId": 0,
                "epoch": 0,
                "samples": 240,
                "bytes": 480,
                "submittedSamples": 480,
                "receivedSequence": 1,
                "receivedSamples": 480,
                "receiveMonotonicMs": 1301,
            },
        ]
    )
    metric = cast("dict[str, object]", timing_metrics(report)["graphRenderedSpeechAge"])
    assert metric["available"] is True
    assert metric["unclassifiableInputPauses"] == 1
    assert metric["withinBounds"] is False


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
    assert "voice continuity was not verified" in str(details["smoothnessConclusion"])


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
