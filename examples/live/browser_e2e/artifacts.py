"""Prepare local speech + a DevTools init script, or unpack captured JSON/WAVs."""

from __future__ import annotations

import argparse
import array
import base64
import binascii
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import wave
from itertools import pairwise
from pathlib import Path
from typing import cast

DEFAULT_TEXT = (
    "Please ask the main assistant to calculate seventeen times twenty three, "
    "explain the calculation, and then tell me the result."
)
REPLY_TEXT = "Seventeen times twenty three is three hundred and ninety one. The audio test is complete."


def speech(path: Path, text: str) -> None:
    """FFmpeg's local Flite synthesizer; no network or provider credentials."""
    executable = shutil.which("ffmpeg")
    if not executable:
        raise SystemExit("Install FFmpeg with the flite filter, or supply --wav and --reply-wav.")
    text_path = path.with_suffix(".txt")
    text_path.write_text(text)
    # Resolve under the generated directory, then use a fixed filter filename.
    subprocess.run(
        [
            executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"flite=textfile={text_path.name}:voice=slt",
            "-ar",
            "24000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            path.name,
        ],
        cwd=path.parent,
        check=True,
    )


def validate_wav(path: Path) -> None:
    with wave.open(str(path), "rb") as source:
        if source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getframerate() != 24000:
            raise SystemExit(f"{path}: expected mono PCM16 WAV at 24000 Hz")
        if not 0 < source.getnframes() <= 24000 * 30:
            raise SystemExit(f"{path}: fixture duration must be between zero and 30 seconds")


def prepare(directory: Path, text: str, wav: str, reply_wav: str, mode: str, max_seconds: int = 120) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    caller, reply = directory / "caller.wav", directory / "reply.wav"
    for destination, provided, content in ((caller, wav, text), (reply, reply_wav, REPLY_TEXT)):
        if provided:
            shutil.copyfile(provided, destination)
        else:
            speech(destination, content)
        validate_wav(destination)
    config = {
        "wavPath": "/assets/voice-e2e-input.wav",
        "fixtureText": text,
        "fixtureSha256": hashlib.sha256(caller.read_bytes()).hexdigest(),
        "mode": mode,
        "maxSeconds": max_seconds,
    }
    script = Path(__file__).with_name("browser-init.js").read_text()
    (directory / "init.js").write_text(f"globalThis.__voiceE2EConfig = {json.dumps(config)};\n{script}")
    (directory / "fixture.json").write_text(
        json.dumps(
            {
                "caller_text": text,
                "reply_text": REPLY_TEXT,
                "caller_sha256": config["fixtureSha256"],
                "reply_sha256": hashlib.sha256(reply.read_bytes()).hexdigest(),
                "mode": mode,
            },
            indent=2,
        )
        + "\n"
    )
    print(directory / "init.js")


def objects(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [cast("dict[str, object]", item) for item in value if isinstance(item, dict)]


def number(value: object, default: float = 0) -> float:
    return float(value) if isinstance(value, (float, int)) else default


def write_wave(path: Path, data: bytes, rate: int) -> None:
    with wave.open(str(path), "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(data)


def silence_spans(data: bytes, rate: int) -> list[dict[str, float]]:
    """Digital silence between audible samples; these are candidates, not proven glitches."""
    samples = array.array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    spans: list[dict[str, float]] = []
    start = -1
    heard = False
    for index, sample in enumerate(samples):
        if abs(sample) <= 2:
            if start < 0 and heard:
                start = index
        else:
            if start >= 0 and index - start >= rate * 0.04:
                spans.append({"startSeconds": start / rate, "durationMs": (index - start) * 1000 / rate})
            start = -1
            heard = True
    return spans


def extract_report(value: object, depth: int = 0) -> dict[str, object]:
    """Accept raw JSON, DevTools fenced text, or MCP content/result envelopes."""
    if depth > 6:
        return {}
    if isinstance(value, dict):
        record = cast("dict[str, object]", value)
        if record.get("schema") == "ngn-browser-voice-e2e/v1":
            return record
        candidates = list(record.values())
    elif isinstance(value, list):
        candidates = cast("list[object]", value)
    elif isinstance(value, str):
        match = re.search(r'\{\s*"schema"\s*:', value)
        try:
            decoded: object
            if match:
                decoded, _ = json.JSONDecoder().raw_decode(value[match.start() :])
            else:
                decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return extract_report(decoded, depth + 1)
    else:
        return {}
    for candidate in candidates:
        found = extract_report(candidate, depth + 1)
        if found:
            return found
    return {}


def unpack(source: Path, directory: Path) -> None:
    report = extract_report(source.read_text())
    if not report:
        raise SystemExit("Expected __voiceE2E.export() JSON, fenced DevTools output, or an MCP content envelope")
    directory.mkdir(parents=True, exist_ok=True)
    inputs = objects(report.get("input"))
    input_data = b"".join(base64.b64decode(str(item["pcm16"])) for item in inputs)
    write_wave(directory / "microphone-transmitted.wav", input_data, 24000)
    captures = objects(report.get("output"))
    context_ids = sorted({int(number(item.get("contextId"))) for item in captures})
    metrics: dict[str, object] = {
        "mode": report.get("mode"),
        "microphoneFrames": len(inputs),
        "microphoneSamples": len(input_data) // 2,
        "outputs": [],
        "omittedEvents": report.get("omitted", 0),
    }
    outputs: list[dict[str, object]] = []
    for context_id in context_ids:
        blocks = sorted(
            (item for item in captures if number(item.get("contextId")) == context_id),
            key=lambda item: number(item.get("frame")),
        )
        rate = int(number(blocks[0].get("sampleRate")))
        result = bytearray()
        cursor = int(number(blocks[0].get("frame")))
        first = cursor
        missing = 0
        for block in blocks:
            frame = int(number(block.get("frame")))
            data = base64.b64decode(str(block["pcm16"]))
            if frame < cursor:
                raise SystemExit("Overlapping output recorder blocks; capture is invalid")
            missing += frame - cursor
            result.extend(bytes((frame - cursor) * 2))
            result.extend(data)
            cursor = frame + len(data) // 2
        filename = f"speaker-rendered-{context_id}.wav"
        write_wave(directory / filename, bytes(result), rate)
        outputs.append(
            {
                "file": filename,
                "sampleRate": rate,
                "firstAudioFrame": first,
                "samples": len(result) // 2,
                "missingRecorderFrames": missing,
                "silenceCandidates": silence_spans(bytes(result), rate),
            }
        )
    metrics["outputs"] = outputs
    health = objects(report.get("health"))
    metrics["lastPlaybackHealth"] = health[-1] if health else {}
    events = objects(report.get("events"))
    metrics["mainThreadGaps"] = [event for event in events if event.get("type") == "main-thread.gap"]
    errors = [event for event in events if "error" in str(event.get("type", "")) or event.get("eventType") == "error"]
    metrics["errors"] = errors
    intervals: dict[str, object] = {}
    for direction in ("send", "receive"):
        times = [number(event.get("monotonicMs")) for event in events if event.get("type") == f"socket.{direction}"]
        gaps = sorted(right - left for left, right in pairwise(times))
        intervals[direction] = {
            "frames": len(times),
            "maxMs": max(gaps, default=0),
            "p95Ms": gaps[min(len(gaps) - 1, int(len(gaps) * 0.95))] if gaps else 0,
        }
    metrics["socketIntervalsIncludingNaturalPauses"] = intervals
    metrics["checks"] = {
        "captureStopped": report.get("stopped") is True,
        "transmittedSpeech": any(number(item.get("rms")) > 0.01 for item in inputs),
        "renderedSpeech": any(number(item.get("rms")) > 0.01 for item in captures),
        "recorderContinuous": bool(outputs) and all(item["missingRecorderFrames"] == 0 for item in outputs),
        "observedDelegationCompletion": any(
            event.get("eventType") == "delegation" and event.get("status") == "completed" for event in events
        ),
        "noObservedErrors": not errors,
    }
    # Keep timestamps and block statistics, remove bulk audio from readable trace.
    for channel in ("input", "output"):
        for block in objects(report.get(channel)):
            block.pop("pcm16", None)
    (directory / "trace.json").write_text(json.dumps(report, indent=2) + "\n")
    (directory / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics, indent=2))


def recorder_gaps(report: dict[str, object]) -> list[dict[str, object]]:
    """Classify only explicitly observed silent pre-speech initialization jumps."""
    events = objects(report.get("events"))
    starts = [
        number(event.get("monotonicMs")) for event in events if event.get("type") in {"fixture.start", "socket.receive"}
    ]
    first_activity = min(starts, default=0)
    blocks = objects(report.get("output"))
    gaps: list[dict[str, object]] = []
    for context_id in {number(block.get("contextId")) for block in blocks}:
        ordered = sorted(
            (block for block in blocks if number(block.get("contextId")) == context_id),
            key=lambda block: number(block.get("frame")),
        )
        for previous, current in pairwise(ordered):
            end = number(previous.get("frame")) + number(previous.get("samples"))
            start = number(current.get("frame"))
            if end == start:
                continue
            observed = [
                event
                for event in events
                if event.get("type") == "recorder.clock-discontinuity"
                and number(event.get("contextId")) == context_id
                and number(event.get("fromFrame")) == end
                and number(event.get("toFrame")) == start
            ]
            startup = (
                bool(observed)
                and start > end
                and all(number(event.get("monotonicMs")) < first_activity for event in observed)
                and number(previous.get("peak")) <= 0.001
                and number(current.get("peak")) <= 0.001
            )
            gaps.append(
                {
                    "contextId": context_id,
                    "fromFrame": end,
                    "toFrame": start,
                    "missingFrames": start - end,
                    "silentInitialization": startup,
                }
            )
    return gaps


def pcm_bytes(block: dict[str, object]) -> bytes:
    encoded = block.get("pcm16")
    if not isinstance(encoded, str):
        return b""
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return b""


def complete_pcm(blocks: list[dict[str, object]], *, output: bool = False) -> bool:
    """Every declared sample must be present, including quiet/middle blocks."""
    if not blocks:
        return False
    for block in blocks:
        samples = block.get("samples")
        if type(samples) not in {int, float}:
            return False
        count = number(samples)
        if not math.isfinite(count) or count <= 0 or not count.is_integer():
            return False
        data = pcm_bytes(block)
        if len(data) % 2 or len(data) != count * 2:
            return False
        if output:
            rate = block.get("sampleRate")
            if type(rate) not in {int, float} or not math.isfinite(number(rate)) or number(rate) <= 0:
                return False
    return True


def captured_signal(blocks: list[dict[str, object]]) -> bool:
    """Require actual PCM bytes, not just a positive producer-reported RMS."""
    for block in blocks:
        data = pcm_bytes(block)
        if not data or len(data) % 2:
            continue
        samples = array.array("h")
        samples.frombytes(data)
        if sys.byteorder != "little":
            samples.byteswap()
        if any(abs(sample) > 327 for sample in samples):
            return True
    return False


def check_capture(
    report: dict[str, object], *, mode: str, reply_samples: int = 0, expect_gap_ms: int = 0, expect_text: str = ""
) -> dict[str, object]:
    """Validate observed behavior; natural speech silence alone never fails a run."""
    events = objects(report.get("events"))
    health = objects(report.get("health"))
    gaps = recorder_gaps(report)
    errors = [
        event
        for event in events
        if "error" in str(event.get("type", ""))
        or event.get("type") == "window.rejection"
        or event.get("eventType") == "error"
        or (event.get("type") == "live.status" and event.get("status") == "error")
        or (event.get("type") == "http.response" and number(event.get("status")) >= 400)
    ]
    last = health[-1] if health else {}
    connected_controls = [
        state
        for event in events
        if event.get("type") == "ui.voice"
        for state in objects(event.get("state"))
        if state.get("selector") == ".voice-controls" and state.get("phase") == "connected"
    ]
    starts = [event for event in events if event.get("type") == "recorder.capture-start"]
    stops = [event for event in events if event.get("type") == "recorder.capture-stop" and event.get("armed") is True]
    recording_jumps = [
        event
        for event in events
        if event.get("type") == "recorder.clock-discontinuity" and event.get("phase") == "recording"
    ]
    outputs = objects(report.get("output"))
    first_frames: dict[float, float] = {}
    final_frames: dict[float, float] = {}
    for block in outputs:
        first_frames.setdefault(number(block.get("contextId")), number(block.get("frame")))
        final_frames[number(block.get("contextId"))] = number(block.get("frame")) + number(block.get("samples"))
    checks = {
        "expectedMode": report.get("mode") == mode,
        "captureStopped": report.get("stopped") is True,
        "transmittedSpeech": captured_signal(objects(report.get("input"))),
        "renderedSpeech": captured_signal(objects(report.get("output"))),
        "inputPcmComplete": complete_pcm(objects(report.get("input"))),
        "outputPcmComplete": complete_pcm(outputs, output=True),
        "noObservedErrors": not errors,
        "noOmittedEvents": number(report.get("omitted")) == 0,
        "measurementWindowStarted": bool(first_frames)
        and all(
            any(
                number(start.get("contextId")) == context_id and number(start.get("frame")) == frame for start in starts
            )
            for context_id, frame in first_frames.items()
        ),
        "measurementWindowStopped": bool(final_frames)
        and all(
            any(
                number(stop.get("contextId")) == context_id and number(stop.get("finalFrame")) == frame
                for stop in stops
            )
            for context_id, frame in final_frames.items()
        ),
        "recorderContinuity": bool(outputs) and not gaps and not recording_jumps,
        "playbackHealthPresent": bool(health),
        "noDroppedSamples": all(number(item.get("droppedSamples")) == 0 for item in health),
        "voiceClosed": any(event.get("type") == "live.status" and event.get("status") == "closed" for event in events),
        "connectedControlsStayLive": bool(connected_controls)
        and all(str(state.get("text", "")).startswith("Live") for state in connected_controls),
    }
    details: dict[str, object] = {
        "recorderGaps": gaps,
        "totalMissingRecorderFrames": sum(number(gap["missingFrames"]) for gap in gaps),
        "errors": errors,
        "warmupClockDiscontinuities": [
            event
            for event in events
            if event.get("type") == "recorder.clock-discontinuity" and event.get("phase") == "warmup"
        ],
        "recordingClockDiscontinuities": recording_jumps,
    }
    if expect_text:
        spoken = " ".join(
            str(event.get("text", ""))
            for event in events
            if event.get("eventType") == "transcript" and event.get("speaker") == "assistant"
        )
        checks["expectedSpokenText"] = " ".join(expect_text.casefold().split()) in " ".join(spoken.casefold().split())
    if mode == "authenticated":
        lifecycles: dict[str, set[str]] = {}
        for event in events:
            if event.get("eventType") == "delegation":
                identifier = str(event.get("delegation_id", ""))
                lifecycles.setdefault(identifier, set()).add(str(event.get("status", "")))
        checks["realDelegationCompleted"] = any(
            identifier and {"queued", "working", "completed"} <= statuses for identifier, statuses in lifecycles.items()
        )
        checks["providerFinalized"] = any(
            "Finalization confirmed." in str(event.get("text", "")) + str(event.get("message", "")) for event in events
        )
        details["delegationStatuses"] = {key: sorted(value) for key, value in lifecycles.items()}
    else:
        checks["knownResponseLength"] = reply_samples > 0 and number(last.get("receivedSamples")) == reply_samples
        checks["allResponseSamplesPlayed"] = reply_samples > 0 and number(last.get("playedSamples")) == reply_samples
        times = [number(event.get("monotonicMs")) for event in events if event.get("type") == "socket.receive"]
        max_interval = max((right - left for left, right in pairwise(times)), default=0)
        premature = 0
        previous = 0
        for item in health:
            count = int(number(item.get("underruns")))
            change = max(0, count - previous)
            if number(item.get("receivedSamples")) < reply_samples or number(item.get("playedSamples")) < reply_samples:
                premature += change
            previous = count
        total = int(number(last.get("underruns")))
        # A single fixed response may exhaust once at EOF. Even if a delayed
        # health callback merges transitions, additional underruns are earlier.
        premature = max(premature, total - 1)
        if expect_gap_ms:
            checks["injectedInterruptionDetected"] = premature > 0 and max_interval >= expect_gap_ms * 0.75
        else:
            checks["noPrematureUnderruns"] = premature == 0
            checks["noMidResponseNetworkGap"] = max_interval < 80
        details.update(
            {
                "totalUnderruns": total,
                "prematureUnderruns": premature,
                "terminalUnderruns": total - premature,
                "maximumReceiveIntervalMs": max_interval,
            }
        )
    return {"passed": all(checks.values()), "checks": checks, "details": details}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("directory", type=Path)
    prepare_parser.add_argument("--text", default=DEFAULT_TEXT)
    prepare_parser.add_argument("--wav", default="")
    prepare_parser.add_argument("--reply-wav", default="")
    prepare_parser.add_argument("--mode", choices=("offline", "authenticated"), default="offline")
    prepare_parser.add_argument("--max-seconds", type=int, default=120, help="Capture window cap, 30 to 180 seconds")
    export_parser = commands.add_parser("unpack")
    export_parser.add_argument("source", type=Path)
    export_parser.add_argument("directory", type=Path)
    check_parser = commands.add_parser("check", help="Exit 1 when the observed E2E contract fails")
    check_parser.add_argument("source", type=Path)
    check_parser.add_argument("--mode", choices=("offline", "authenticated"), required=True)
    check_parser.add_argument("--reply-wav", type=Path)
    check_parser.add_argument("--expect-gap-ms", type=int, default=0)
    check_parser.add_argument("--expect-text", default="")
    args = parser.parse_args()
    if args.command == "prepare":
        if not 30 <= args.max_seconds <= 180:
            parser.error("--max-seconds must be between 30 and 180")
        prepare(args.directory.resolve(), args.text, args.wav, args.reply_wav, args.mode, args.max_seconds)
    elif args.command == "unpack":
        unpack(args.source, args.directory)
    else:
        report = extract_report(args.source.read_text())
        if not report:
            raise SystemExit("Expected an unmodified browser capture with PCM data")
        reply_samples = 0
        if args.reply_wav:
            validate_wav(args.reply_wav)
            with wave.open(str(args.reply_wav), "rb") as source:
                reply_samples = source.getnframes()
        if args.mode == "offline" and not reply_samples:
            parser.error("offline check requires --reply-wav with the known single response")
        result = check_capture(
            report,
            mode=args.mode,
            reply_samples=reply_samples,
            expect_gap_ms=args.expect_gap_ms,
            expect_text=args.expect_text,
        )
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
