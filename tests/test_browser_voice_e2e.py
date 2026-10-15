"""Artifact validation must distinguish a real interruption from terminal EOF."""

import base64
import copy
import json
import struct
from typing import cast

from examples.live.browser_e2e.artifacts import check_capture
from examples.live.browser_e2e.artifacts import extract_report


def block(frame: int = 0, *, silent: bool = False) -> dict[str, object]:
    return {
        "contextId": 0,
        "frame": frame,
        "samples": 4,
        "sampleRate": 48000,
        "peak": 0 if silent else 0.05,
        "pcm16": base64.b64encode(struct.pack("<4h", *([0 if silent else 1000] * 4))).decode(),
    }


def baseline() -> dict[str, object]:
    return {
        "schema": "ngn-browser-voice-e2e/v1",
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
            {"type": "recorder.capture-start", "contextId": 0, "frame": 0, "monotonicMs": 20},
            {"type": "recorder.capture-stop", "contextId": 0, "finalFrame": 4, "armed": True, "monotonicMs": 200},
        ],
    }


def test_terminal_eof_is_not_an_in_speech_glitch() -> None:
    result = check_capture(baseline(), mode="offline", reply_samples=4)
    assert result["passed"] is True
    assert cast("dict[str, object]", result["details"])["terminalUnderruns"] == 1


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


def test_warmup_clock_jumps_remain_visible_and_post_arm_jumps_fail() -> None:
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
    assert check_capture(report, mode="offline", reply_samples=4)["passed"] is False
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
    cast("list[dict[str, object]]", report["events"])[-1]["finalFrame"] = 8
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
