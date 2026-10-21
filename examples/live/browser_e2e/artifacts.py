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
from bisect import bisect_left
from bisect import bisect_right
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


def percentile(values: list[float], fraction: float = 0.95) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))] if ordered else 0


def normalized_text(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def expected_phrases(value: object) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def render_monotonic_ms(block: dict[str, object], sample_offset: int = 0) -> float:
    """Audio-frame position, aligned with the browser's output clock when available."""
    rate = number(block.get("sampleRate"))
    if rate <= 0:
        return number(block.get("monotonicMs"))
    audio_time = (number(block.get("frame")) + sample_offset) / rate
    timestamp = block.get("outputTimestamp")
    if isinstance(timestamp, dict) and number(timestamp.get("performanceTime")) > 0:
        return number(timestamp.get("performanceTime")) + (audio_time - number(timestamp.get("contextTime"))) * 1000
    return number(block.get("monotonicMs")) + (audio_time - number(block.get("contextTimeAtDelivery"))) * 1000


def _audio_time_monotonic_ms(context_time: float, stamp: dict[str, object], fallback_ms: float) -> float:
    timestamp = stamp.get("outputTimestamp")
    if isinstance(timestamp, dict) and number(timestamp.get("performanceTime")) > 0:
        return number(timestamp.get("performanceTime")) + (context_time - number(timestamp.get("contextTime"))) * 1000
    delivery_time = number(stamp.get("contextTimeAtDelivery"), float("nan"))
    if not math.isfinite(delivery_time):
        return float("nan")
    return fallback_ms + (context_time - delivery_time) * 1000


def _graph_time_monotonic_ms(context_time: float, stamp: dict[str, object]) -> float:
    """Map a context frame to its graph clock, excluding device presentation offset."""
    delivery_time = number(stamp.get("contextTimeAtDelivery"), float("nan"))
    delivery_ms = number(stamp.get("monotonicMs"), float("nan"))
    if not math.isfinite(delivery_time) or not math.isfinite(delivery_ms):
        return float("nan")
    return delivery_ms + (context_time - delivery_time) * 1000


def received_pcm_ledger_complete(report: dict[str, object]) -> bool:
    """Validate the complete received-PCM ledger without writing an artifact."""
    packets_value, summary_value = report.get("received"), report.get("receivedPcm")
    if not isinstance(packets_value, list) or not isinstance(summary_value, dict):
        return False
    integer_fields = (
        "packetCount",
        "capturedPackets",
        "sampleRate",
        "samples",
        "capturedSamples",
        "capturedBytes",
        "omittedPackets",
        "omittedBytes",
        "byteLimit",
    )
    if (
        summary_value.get("sampleFormat") != "s16le"
        or any(type(summary_value.get(key)) is not int for key in integer_fields)
        or summary_value.get("sampleRate") != 24000
        or summary_value.get("omittedPackets") != 0
        or summary_value.get("omittedBytes") != 0
        or not 0 < summary_value["byteLimit"] <= 30_000_000
        or type(report.get("omitted")) is not int
        or report.get("omitted") != 0
        or not 0 < len(packets_value) <= 100_000
    ):
        return False
    summary = cast("dict[str, int]", summary_value)
    samples_total = bytes_total = previous_transport = 0
    previous_transport_samples: int | None = None
    previous_capture_order = -1
    context_id: int | None = None
    transports: set[int] = set()
    for index, packet in enumerate(packets_value):
        if not isinstance(packet, dict):
            return False
        encoded = packet.get("pcm16")
        if not isinstance(encoded, str) or len(encoded) > 64_000:
            return False
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return False
        count = packet.get("samples")
        transport = packet.get("transportSequence")
        transport_received = packet.get("transportReceivedSamples")
        current_context, epoch = packet.get("contextId"), packet.get("epoch")
        capture_order, sequence = packet.get("captureOrder"), packet.get("sequence")
        if (
            type(count) is not int
            or count <= 0
            or count > 24_000
            or len(raw) > 48_000
            or len(raw) != count * 2
            or type(packet.get("sampleRate")) is not int
            or packet.get("sampleRate") != 24000
            or type(packet.get("sampleOffset")) is not int
            or packet.get("sampleOffset") != samples_total
            or type(packet.get("receivedSamples")) is not int
            or packet.get("receivedSamples") != samples_total + count
            or type(packet.get("bytes")) is not int
            or packet.get("bytes") != len(raw)
            or type(sequence) is not int
            or sequence != index
            or type(transport) is not int
            or transport < 0
            or transport in transports
            or (index and transport != previous_transport + 1)
            or type(transport_received) is not int
            or transport_received < count
            or (previous_transport_samples is not None and transport_received != previous_transport_samples + count)
            or type(current_context) is not int
            or current_context < 0
            or type(epoch) is not int
            or epoch < 0
            or type(capture_order) is not int
            or capture_order <= previous_capture_order
            or not math.isfinite(number(packet.get("monotonicMs"), float("nan")))
        ):
            return False
        if context_id is None:
            context_id = current_context
        elif current_context != context_id:
            return False
        transports.add(transport)
        previous_transport = transport
        previous_transport_samples = transport_received
        previous_capture_order = capture_order
        samples_total += count
        bytes_total += len(raw)
        if bytes_total > min(30_000_000, summary["byteLimit"]):
            return False
    return (
        summary["packetCount"] == len(packets_value)
        and summary["capturedPackets"] == len(packets_value)
        and summary["samples"] == samples_total
        and summary["capturedSamples"] == samples_total
        and summary["capturedBytes"] == bytes_total
    )


def _source_offset_time(at: float, until: float, offset: int, samples: int) -> float:
    return at + (until - at) * offset / max(1, samples - 1)


def _normalized_pcm_correlation(
    source: array.array[int],
    rendered: array.array[int],
    offsets: list[int],
    at: float,
    until: float,
    samples: int,
    output_rate: float,
    output_origin: int,
    shift: int,
) -> float:
    pairs: list[tuple[float, float]] = []
    for offset in offsets:
        source_value = float(source[offset])
        output_frame = round(_source_offset_time(at, until, offset, samples) * output_rate) + shift - output_origin
        if 0 <= output_frame < len(rendered):
            pairs.append((source_value, float(rendered[output_frame])))
    minimum_pairs = min(8, max(2, len(offsets) // 4))
    if len(pairs) < minimum_pairs:
        return -1.0
    source_mean = sum(left for left, _ in pairs) / len(pairs)
    rendered_mean = sum(right for _, right in pairs) / len(pairs)
    source_energy = sum((left - source_mean) ** 2 for left, _ in pairs)
    rendered_energy = sum((right - rendered_mean) ** 2 for _, right in pairs)
    if source_energy <= 0 or rendered_energy <= 0:
        return -1.0
    covariance = sum((left - source_mean) * (right - rendered_mean) for left, right in pairs)
    return covariance / math.sqrt(source_energy * rendered_energy)


def graph_rendered_speech_age(report: dict[str, object]) -> dict[str, object]:
    """Validate traced source PCM against the software-sink post-volume graph."""
    events = objects(report.get("events"))
    all_trace = [event for event in events if event.get("type") == "playback.trace"]
    starts = [event for event in events if event.get("type") == "measurement.start"]
    requests = [event for event in events if event.get("type") == "measurement.checkpoint-request"]
    checkpoints = [event for event in events if event.get("type") == "playback.trace-checkpoint"]
    ends = [event for event in events if event.get("type") == "measurement.end"]
    measurement_start = starts[0] if len(starts) == 1 else {}
    begin_requests = [event for event in requests if event.get("phase") == "begin"]
    end_requests = [event for event in requests if event.get("phase") == "end"]
    begin_request = begin_requests[0] if len(begin_requests) == 1 else {}
    request = end_requests[0] if len(end_requests) == 1 else {}
    begin_request_id = begin_request.get("requestId")
    end_request_id = request.get("requestId")
    begin_replies = [event for event in checkpoints if event.get("requestId") == begin_request_id]
    end_replies = [event for event in checkpoints if event.get("requestId") == end_request_id]
    begin_checkpoint = begin_replies[0] if len(begin_replies) == 1 else {}
    checkpoint = end_replies[0] if len(end_replies) == 1 else {}
    end = ends[0] if len(ends) == 1 else {}
    received_summary = report.get("receivedPcm")
    context_rate = number(
        next(
            (
                item.get("sampleRate")
                for item in objects(report.get("contexts"))
                if item.get("id") == measurement_start.get("contextId") and type(item.get("sampleRate")) is int
            ),
            0,
        )
    )
    max_checkpoint_overshoot = math.ceil(128 * 24000 / context_rate) if context_rate > 0 else -1
    begin_accepted = begin_checkpoint.get("acceptedEnd")
    begin_received = begin_checkpoint.get("receivedEnd")
    begin_rendered = begin_checkpoint.get("renderedEnd")
    summary_samples = received_summary.get("samples") if isinstance(received_summary, dict) else None
    start_received = measurement_start.get("transportReceivedSamplesAtStart")
    accepted = checkpoint.get("acceptedEnd")
    received_at_checkpoint = checkpoint.get("receivedEnd")
    rendered = checkpoint.get("renderedEnd")
    end_received = end.get("transportReceivedSamples")
    checkpoint_valid = (
        bool(measurement_start)
        and bool(begin_request)
        and bool(begin_checkpoint)
        and bool(request)
        and bool(checkpoint)
        and bool(end)
        and begin_request.get("requestId") == measurement_start.get("beginRequestId")
        and begin_request.get("contextId") == measurement_start.get("contextId")
        and begin_request.get("epoch") == measurement_start.get("epoch")
        and type(begin_request.get("submittedEnd")) is int
        and type(begin_request.get("transportReceivedSamples")) is int
        and type(begin_request_id) is int
        and begin_request_id >= 0
        and begin_request.get("submittedEnd") == begin_request.get("transportReceivedSamples")
        and begin_checkpoint.get("status") == "completed"
        and begin_checkpoint.get("contextId") == begin_request.get("contextId")
        and begin_checkpoint.get("epoch") == begin_request.get("epoch")
        and begin_checkpoint.get("requestId") == begin_request.get("requestId")
        and type(begin_accepted) is int
        and type(begin_checkpoint.get("omittedIntervals")) is int
        and begin_accepted == begin_request.get("submittedEnd")
        and type(begin_received) is int
        and type(begin_rendered) is int
        and begin_received >= begin_accepted
        and isinstance(received_summary, dict)
        and type(summary_samples) is int
        and begin_received <= summary_samples
        and begin_rendered <= begin_received
        and begin_rendered >= begin_accepted
        and begin_rendered - begin_accepted <= max_checkpoint_overshoot
        and begin_rendered == measurement_start.get("sourceStart")
        and begin_checkpoint.get("omittedIntervals") == 0
        and measurement_start.get("beginAcceptedEnd") == begin_accepted
        and type(start_received) is int
        and start_received >= begin_received
        and type(end_request_id) is int
        and end_request_id >= 0
        and end_request_id == checkpoint.get("requestId") == end.get("requestId")
        and measurement_start.get("contextId")
        == request.get("contextId")
        == checkpoint.get("contextId")
        == end.get("contextId")
        and measurement_start.get("epoch") == request.get("epoch") == checkpoint.get("epoch") == end.get("epoch")
        and checkpoint.get("status") == "completed"
        and type(measurement_start.get("sourceStart")) is int
        and type(accepted) is int
        and type(rendered) is int
        and type(received_at_checkpoint) is int
        and type(request.get("submittedEnd")) is int
        and type(request.get("transportReceivedSamples")) is int
        and accepted == request.get("submittedEnd")
        and request.get("submittedEnd") == request.get("transportReceivedSamples")
        and received_at_checkpoint >= accepted
        and isinstance(received_summary, dict)
        and type(summary_samples) is int
        and received_at_checkpoint <= summary_samples
        and rendered <= received_at_checkpoint
        and rendered >= accepted
        and rendered - accepted <= max_checkpoint_overshoot
        and type(checkpoint.get("omittedIntervals")) is int
        and checkpoint.get("omittedIntervals") == 0
        and end.get("renderedEnd") == rendered
        and end.get("acceptedEnd") == accepted
        and type(end_received) is int
        and end_received >= received_at_checkpoint
    )
    trace = []
    boundary_coordinate_errors = 0
    if checkpoint_valid:
        measured_prefix_start = cast("int", measurement_start["sourceStart"])
        measured_prefix_end = cast("int", rendered)
        target_context, target_epoch = measurement_start.get("contextId"), measurement_start.get("epoch")
        for item in all_trace:
            source_start, count = item.get("sourceStart"), item.get("samples")
            if (
                item.get("contextId") != target_context
                or item.get("epoch") != target_epoch
                or type(source_start) is not int
                or type(count) is not int
                or count <= 0
            ):
                continue
            left, right = max(measured_prefix_start, source_start), min(measured_prefix_end, source_start + count)
            if right <= left:
                continue
            if source_start < measured_prefix_start < source_start + count:
                boundary_coordinate_errors += 1
            if source_start < measured_prefix_end < source_start + count:
                boundary_coordinate_errors += 1
            clipped = dict(item)
            if left != source_start or right != source_start + count:
                at, until = number(item.get("at"), float("nan")), number(item.get("until"), float("nan"))
                clipped["at"] = at + (until - at) * (left - source_start) / max(1, count - 1)
                clipped["until"] = at + (until - at) * (right - 1 - source_start) / max(1, count - 1)
                clipped["sourceStart"], clipped["samples"] = left, right - left
            trace.append(clipped)
    output_blocks = objects(report.get("output"))
    output_complete = complete_pcm(output_blocks, output=True) and recorder_counters_continuous(output_blocks)
    received_complete = received_pcm_ledger_complete(report)
    packets_by_transport: dict[int, tuple[array.array[int], int, float, object, object]] = {}
    receive_capture_orders: dict[int, int] = {}
    receive_transport_ends: dict[int, int] = {}
    duplicate_received_sequences = 0
    beginning = number(measurement_start.get("monotonicMs"))
    for packet in objects(report.get("received")):
        transport = packet.get("transportSequence")
        raw = pcm_bytes(packet)
        if type(transport) is not int or not raw or len(raw) % 2:
            continue
        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
            samples.byteswap()
        if type(packet.get("sampleOffset")) is not int:
            continue
        if transport in packets_by_transport:
            duplicate_received_sequences += 1
            continue
        packets_by_transport[transport] = (
            samples,
            int(number(packet.get("sampleOffset"))),
            number(packet.get("monotonicMs"), float("nan")),
            packet.get("contextId"),
            packet.get("epoch"),
        )
        if type(packet.get("captureOrder")) is int:
            receive_capture_orders[transport] = cast("int", packet["captureOrder"])
        if type(packet.get("receivedSamples")) is int:
            receive_transport_ends[transport] = cast("int", packet["receivedSamples"])

    submissions: list[dict[str, object]] = []
    invalid_submissions = 0
    submitted_transports: set[int] = set()
    for event in events:
        if event.get("type") != "playback.submit":
            continue
        count, submitted_end, received_sequence = (
            event.get("samples"),
            event.get("submittedSamples"),
            event.get("receivedSequence"),
        )
        if (
            type(count) is not int
            or count <= 0
            or type(submitted_end) is not int
            or submitted_end < count
            or type(received_sequence) is not int
            or received_sequence not in packets_by_transport
            or event.get("bytes") != count * 2
        ):
            invalid_submissions += 1
            continue
        source, receive_offset, receive_ms, receive_context, receive_epoch = packets_by_transport[received_sequence]
        if (
            len(source) != count
            or not math.isfinite(receive_ms)
            or receive_context != event.get("contextId")
            or receive_epoch != event.get("epoch")
        ):
            invalid_submissions += 1
            continue
        if received_sequence in submitted_transports:
            invalid_submissions += 1
            continue
        submitted_transports.add(received_sequence)
        submissions.append(
            {
                "contextId": event.get("contextId"),
                "epoch": event.get("epoch"),
                "start": submitted_end - count,
                "end": submitted_end,
                "source": source,
                "receivedOffset": receive_offset,
                "receiveMs": receive_ms,
            }
        )
    submissions.sort(key=lambda item: (number(item.get("contextId")), number(item.get("start"))))
    checkpoint_order = request.get("captureOrder") if checkpoint_valid else None
    measured_transport_sequences = {
        transport
        for transport in packets_by_transport
        if receive_transport_ends.get(transport, 0) > number(measurement_start.get("transportReceivedSamplesAtStart"))
        and (
            type(checkpoint_order) is not int
            or receive_capture_orders.get(transport, checkpoint_order + 1) <= checkpoint_order
        )
    }
    unsubmitted_received_packets = len(measured_transport_sequences - submitted_transports)
    source_starts: dict[int, list[int]] = {}
    source_ranges: dict[int, list[dict[str, object]]] = {}
    for submission in submissions:
        context_id = submission.get("contextId")
        if type(context_id) is int:
            source_starts.setdefault(context_id, []).append(int(number(submission.get("start"))))
            source_ranges.setdefault(context_id, []).append(submission)

    # Decode the single software-sink stream once; samples are retained in an
    # array rather than Python integers to bound memory for long captures.
    output_contexts = {block.get("contextId") for block in output_blocks}
    candidate_context = next(iter(output_contexts)) if len(output_contexts) == 1 else None
    output_context: int | None = candidate_context if type(candidate_context) is int else None
    output_rate_value = output_blocks[0].get("sampleRate") if output_blocks else None
    output_rate = number(output_blocks[0].get("sampleRate")) if output_blocks else 0
    rates_valid = output_context is not None and type(output_rate_value) is int and output_rate >= 8000
    context_rates: dict[int, int] = {}
    for context in objects(report.get("contexts")):
        context_id, rate = context.get("id"), context.get("sampleRate")
        if (
            type(context_id) is not int
            or type(rate) is not int
            or context_id in context_rates
            or not 8000 <= rate <= 192000
        ):
            rates_valid = False
            continue
        context_rates[context_id] = rate
    if output_context is None or context_rates.get(output_context) != output_rate_value:
        rates_valid = False
    output_complete = output_complete and rates_valid
    output_samples = array.array("h")
    output_origin = 0
    if output_blocks and output_context is not None:
        output_origin = int(number(output_blocks[0].get("frame")))
        for block in output_blocks:
            raw = pcm_bytes(block)
            if not raw or len(raw) % 2:
                output_complete = False
                continue
            values = array.array("h")
            values.frombytes(raw)
            if sys.byteorder != "little":
                values.byteswap()
            output_samples.extend(values)

    batch_intervals: dict[tuple[object, object, object], int] = {}
    observed_batch_counts: dict[tuple[object, object, object], int] = {}
    batch_omissions: dict[tuple[object, object, object], int] = {}
    sequence_errors = 0
    omitted_intervals = 0
    for event in all_trace:
        key = (event.get("contextId"), event.get("epoch"), event.get("sequence"))
        expected_count = event.get("batchIntervals")
        if (
            type(expected_count) is not int
            or not 1 <= expected_count <= 10
            or (key in batch_intervals and batch_intervals[key] != expected_count)
        ):
            sequence_errors += 1
        else:
            batch_intervals[key] = expected_count
        observed_batch_counts[key] = observed_batch_counts.get(key, 0) + 1
        omitted = event.get("omittedIntervals")
        if type(omitted) is not int or omitted < 0:
            sequence_errors += 1
        else:
            if key in batch_omissions and batch_omissions[key] != omitted:
                sequence_errors += 1
            batch_omissions[key] = omitted
            omitted_intervals = max(omitted_intervals, omitted)
    sequence_by_epoch: dict[tuple[object, object], list[int]] = {}
    for context_id, epoch, sequence in batch_intervals:
        if type(context_id) is not int or type(epoch) is not int or type(sequence) is not int or sequence < 0:
            sequence_errors += 1
            continue
        if observed_batch_counts[(context_id, epoch, sequence)] != batch_intervals[(context_id, epoch, sequence)]:
            sequence_errors += 1
        sequence_by_epoch.setdefault((context_id, epoch), []).append(sequence)
    for sequences in sequence_by_epoch.values():
        ordered = sorted(set(sequences))
        if ordered != list(range(len(ordered))):
            sequence_errors += 1

    ordered_trace = sorted(trace, key=lambda item: number(item.get("captureOrder"), float("inf")))
    trace_ends: dict[tuple[object, object], int] = {}
    invalid_intervals = coverage_errors = source_unmapped = output_unconfirmed = 0
    ages: list[float] = []
    age_minutes: dict[int, list[float]] = {}
    gap_excesses: list[float] = []
    gated_gap_excesses: list[float] = []
    long_source_gaps = 0
    unclassifiable_input_pauses = 0
    previous_voiced: dict[int, tuple[int, float, float]] = {}
    contexts = {item.get("id"): number(item.get("sampleRate")) for item in objects(report.get("contexts"))}
    for event in ordered_trace:
        context_id, epoch = event.get("contextId"), event.get("epoch")
        start, count = event.get("sourceStart"), event.get("samples")
        at_value, until_value = event.get("at"), event.get("until")
        at, until = number(at_value, float("nan")), number(until_value, float("nan"))
        context_rate = contexts.get(context_id, 0)
        if (
            type(context_id) is not int
            or type(epoch) is not int
            or type(start) is not int
            or type(count) is not int
            or not 0 < count <= 480
            or start < 0
            or type(at_value) not in {int, float}
            or type(until_value) not in {int, float}
            or not math.isfinite(at)
            or not math.isfinite(until)
            or at < 0
            or until < at
            or context_rate <= 0
            or abs((until - at) - (count - 1) / 24000) > (128 / context_rate + (count - 1) / 24000 * 0.001)
        ):
            invalid_intervals += 1
            continue
        trace_key = (context_id, epoch)
        if trace_key in trace_ends and start != trace_ends[trace_key]:
            coverage_errors += 1
        trace_ends[trace_key] = start + count
        render_start_ms = _graph_time_monotonic_ms(at, event)
        if not math.isfinite(render_start_ms):
            invalid_intervals += 1
            continue
        if render_start_ms < beginning:
            continue
        starts_for_context = source_starts.get(context_id, [])
        ranges_for_context = source_ranges.get(context_id, [])
        source_values = array.array("h")
        arrival_for_offset: dict[int, float] = {}
        cursor = start
        while cursor < start + count:
            range_index = bisect_right(starts_for_context, cursor) - 1
            if range_index < 0:
                break
            submission = ranges_for_context[range_index]
            range_start = int(number(submission.get("start")))
            range_end = int(number(submission.get("end")))
            if cursor >= range_end:
                break
            source_packet = cast("array.array[int]", submission["source"])
            take = min(start + count, range_end) - cursor
            packet_offset = cursor - range_start
            source_values.extend(source_packet[packet_offset : packet_offset + take])
            for offset in range(cursor - start, cursor - start + take):
                arrival_for_offset[offset] = number(submission.get("receiveMs"), float("nan"))
            cursor += take
        if cursor != start + count or len(source_values) != count:
            if number(event.get("monotonicMs")) >= min(
                (number(item.get("monotonicMs")) for item in events if item.get("type") == "measurement.start"),
                default=0,
            ):
                source_unmapped += 1
            continue
        active_offsets = [offset for offset in range(count) if abs(source_values[offset]) > 327]
        if not active_offsets:
            continue

        # Confirm the source waveform exists in the actual post-volume graph.
        # Sparse signed samples make this gain/attenuation tolerant without an
        # external DSP dependency; search only within one output quantum.
        if output_context != context_id or output_rate <= 0:
            output_unconfirmed += 1
            continue
        # Retain zero samples: they provide useful context for short, isolated
        # transients and prevent a few loud points from defining a whole span.
        offsets = list(range(count))
        best_correlation = max(
            _normalized_pcm_correlation(
                source_values, output_samples, offsets, at, until, count, output_rate, output_origin, shift
            )
            for shift in range(-2, 3)
        )
        if best_correlation < 0.9:
            best_correlation = max(
                _normalized_pcm_correlation(
                    source_values, output_samples, offsets, at, until, count, output_rate, output_origin, shift
                )
                for shift in range(-128, 129)
            )
        if best_correlation < 0.65:
            output_unconfirmed += 1
            continue

        for offset in active_offsets[::24]:
            coordinate = start + offset
            arrival_ms = arrival_for_offset.get(offset, float("nan"))
            render_context_time = _source_offset_time(at, until, offset, count)
            render_ms = _graph_time_monotonic_ms(render_context_time, event)
            if not math.isfinite(render_ms) or not math.isfinite(arrival_ms):
                invalid_intervals += 1
                break
            age = render_ms - arrival_ms
            if age < -2 or not math.isfinite(age):
                invalid_intervals += 1
                break
            age = max(0, age)
            ages.append(age)
            minute = max(0, int((render_ms - beginning) // 60000))
            age_minutes.setdefault(minute, []).append(age)
            prior = previous_voiced.get(context_id)
            if prior is not None and coordinate > prior[0]:
                source_gap_ms = (coordinate - prior[0]) / 24
                # Compare in the worklet's context clock; performance callback
                # jitter between batches must not appear as an audio gap.
                render_gap_ms = (render_context_time - prior[1]) * 1000
                excess = render_gap_ms - source_gap_ms
                gap_excesses.append(excess)
                receive_gap_ms = arrival_ms - prior[2]
                if source_gap_ms > 200:
                    # Retain long phrase/turn pauses as diagnostics; their
                    # silence is expected source content, not starvation.
                    long_source_gaps += 1
                elif source_gap_ms < 40 and receive_gap_ms > 200:
                    # With no silent source samples, a long packet gap cannot
                    # distinguish provider DTX from a transport interruption.
                    unclassifiable_input_pauses += 1
                else:
                    gated_gap_excesses.append(excess)
            previous_voiced[context_id] = (coordinate, render_context_time, arrival_ms)

    prefix_context = measurement_start.get("contextId")
    prefix_epoch = measurement_start.get("epoch")
    result_prefix_start = measurement_start.get("sourceStart")
    result_prefix_end = checkpoint.get("renderedEnd")
    prefix_coordinate_errors = 0
    if checkpoint_valid:
        prefix_key = (prefix_context, prefix_epoch)
        prefix_coordinate_errors = int(
            prefix_key not in trace_ends
            or trace_ends.get(prefix_key) != result_prefix_end
            or not trace
            or min(int(number(item.get("sourceStart"))) for item in trace) != result_prefix_start
        )
    else:
        prefix_coordinate_errors = 1
    omitted_events = number(report.get("omitted"))
    available = (
        bool(ages)
        and received_complete
        and output_complete
        and omitted_events == 0
        and not any(
            (
                invalid_submissions,
                duplicate_received_sequences,
                unsubmitted_received_packets,
                sequence_errors,
                omitted_intervals,
                invalid_intervals,
                coverage_errors,
                source_unmapped,
                output_unconfirmed,
                prefix_coordinate_errors,
                boundary_coordinate_errors,
            )
        )
    )
    minute_windows = [
        {"minute": minute, "observations": len(values), "p95Ms": percentile(values), "maxMs": max(values)}
        for minute, values in sorted(age_minutes.items())
    ]
    post_checkpoint_received_samples = (
        max(0, int(number(received_summary.get("samples")) - number(checkpoint.get("receivedEnd"))))
        if isinstance(received_summary, dict) and checkpoint_valid
        else None
    )
    accepted_during_checkpoint_samples = (
        max(0, int(number(checkpoint.get("receivedEnd")) - number(checkpoint.get("acceptedEnd"))))
        if checkpoint_valid
        else None
    )
    post_prefix_trace_intervals = max(0, len(all_trace) - len(trace)) if checkpoint_valid else None
    checkpoint_output_frame = round(number(checkpoint.get("renderedAt")) * output_rate) if checkpoint_valid else 0
    post_checkpoint_output_samples = (
        max(0, output_origin + len(output_samples) - checkpoint_output_frame)
        if checkpoint_valid and output_rate > 0
        else None
    )
    return {
        "available": available,
        "clock": "software_output_graph",
        "receivedPcmComplete": received_complete,
        "duplicateReceivedSequences": duplicate_received_sequences,
        "unsubmittedReceivedPackets": unsubmitted_received_packets,
        "outputPcmComplete": output_complete,
        "traceBatches": len(batch_intervals),
        "traceIntervals": len(trace),
        "traceSequenceErrors": sequence_errors,
        "omittedIntervals": omitted_intervals,
        "invalidIntervals": invalid_intervals,
        "coverageErrors": coverage_errors,
        "invalidSubmissions": invalid_submissions,
        "unmappedSourceIntervals": source_unmapped,
        "uncorrelatedOutputIntervals": output_unconfirmed,
        "tailCoordinateErrors": prefix_coordinate_errors,
        "checkpointBoundaryCoordinateErrors": boundary_coordinate_errors,
        "checkpointValid": checkpoint_valid,
        "checkpointContextId": prefix_context,
        "checkpointEpoch": prefix_epoch,
        "checkpointSourceStart": result_prefix_start,
        "checkpointRenderedEnd": result_prefix_end,
        "postCheckpointReceivedSamples": post_checkpoint_received_samples,
        "receivedSamplesAcceptedDuringCheckpoint": accepted_during_checkpoint_samples,
        "traceIntervalsOutsideMeasuredPrefix": post_prefix_trace_intervals,
        "approxPostCheckpointOutputSamples": post_checkpoint_output_samples,
        "maximumAddedVoicedGapMs": max(gap_excesses, default=0),
        "maximumClassifiableAddedGapMs": max(gated_gap_excesses, default=0),
        "longSourcePauseGaps": long_source_gaps,
        "unclassifiableInputPauses": unclassifiable_input_pauses,
        "voicedGapOver40Ms": any(gap > 40 for gap in gated_gap_excesses),
        "observations": len(ages),
        "p95Ms": percentile(ages) if available else None,
        "maxMs": max(ages, default=0) if available else None,
        "minuteWindows": minute_windows,
        "withinBounds": available
        and percentile(ages) <= 250
        and max(ages, default=0) <= 1000
        and not any(gap > 40 for gap in gated_gap_excesses)
        and unclassifiable_input_pauses == 0,
        "voiceContinuityVerified": available
        and not any(gap > 40 for gap in gated_gap_excesses)
        and unclassifiable_input_pauses == 0,
        "meaning": "Received source PCM to matching post-volume software-sink graph render; not acoustic or device presentation latency.",
    }


def rendered_speech_age(report: dict[str, object]) -> dict[str, object]:
    """Map active rendered PCM back to packet arrival, requiring audible output evidence."""
    events = objects(report.get("events"))
    submissions: dict[tuple[int, int], list[tuple[int, int, float]]] = {}
    invalid_submissions: set[tuple[object, object]] = set()
    submission_ends: dict[tuple[object, object], int] = {}
    invalid_submission_events = 0
    for event in events:
        if event.get("type") != "playback.submit":
            continue
        key = (event.get("contextId"), event.get("epoch"))
        if type(key[0]) is not int or key[0] < 0 or type(key[1]) is not int or key[1] < 0:
            invalid_submission_events += 1
            continue
        samples = event.get("samples")
        end = event.get("submittedSamples")
        received = number(event.get("receiveMonotonicMs"), float("nan"))
        start = end - samples if type(end) is int and type(samples) is int else -1
        if (
            type(samples) is not int
            or samples <= 0
            or type(end) is not int
            or start < 0
            or not math.isfinite(received)
            or event.get("bytes") != samples * 2
        ):
            invalid_submissions.add(key)
            continue
        if key in submission_ends and start != submission_ends[key]:
            invalid_submissions.add(key)
        submission_ends[key] = end
        typed_key = (key[0], key[1])
        submissions.setdefault(typed_key, []).append((start, end, received))
    submission_starts = {key: [low for low, _, _ in ranges] for key, ranges in submissions.items()}
    output_runs: dict[int, list[tuple[float, float]]] = {}
    invalid_output_contexts = 0
    for block_item in objects(report.get("output")):
        rate = number(block_item.get("sampleRate"))
        frame = block_item.get("frame")
        context_id = block_item.get("contextId")
        if type(context_id) is not int or context_id < 0:
            invalid_output_contexts += 1
            continue
        if rate <= 0 or type(frame) is not int:
            continue
        raw = pcm_bytes(block_item)
        if len(raw) % 2:
            continue
        samples = array.array("h")
        samples.frombytes(raw)
        if sys.byteorder != "little":
            samples.byteswap()
        if len(samples) != block_item.get("samples"):
            continue
        run_start: int | None = None
        for offset in range(len(samples) + 1):
            active = offset < len(samples) and abs(samples[offset]) > 327
            if active and run_start is None:
                run_start = offset
            elif not active and run_start is not None:
                start_ms = render_monotonic_ms(block_item, run_start)
                end_ms = render_monotonic_ms(block_item, offset - 1)
                output_runs.setdefault(context_id, []).append((start_ms, end_ms))
                run_start = None
    for runs in output_runs.values():
        runs.sort()
    output_blocks = objects(report.get("output"))
    output_complete = complete_pcm(output_blocks, output=True) and recorder_counters_continuous(output_blocks)
    context_rates: dict[int, float] = {}
    context_rate_valid = True
    for context in objects(report.get("contexts")):
        context_id = context.get("id")
        sample_rate = number(context.get("sampleRate"), float("nan"))
        if (
            type(context_id) is not int
            or context_id < 0
            or type(context.get("sampleRate")) is not int
            or not 8000 <= sample_rate <= 192000
        ):
            context_rate_valid = False
            continue
        if context_id in context_rates:
            context_rate_valid = False
        context_rates[context_id] = sample_rate

    def has_output(context_id: int, start: float, end: float, tolerance_ms: float) -> bool:
        runs = output_runs.get(context_id, [])
        start -= tolerance_ms
        end += tolerance_ms
        index = bisect_left(runs, (start, float("-inf")))
        if index > 0 and runs[index - 1][1] >= start:
            return True
        return index < len(runs) and runs[index][0] <= end

    ages: list[float] = []
    beginning = min(
        (number(event.get("monotonicMs")) for event in events if event.get("type") == "measurement.start"),
        default=min(
            (number(event.get("monotonicMs")) for event in events if event.get("type") == "fixture.start"),
            default=0,
        ),
    )
    minute_ages: dict[int, list[float]] = {}
    active_blocks = silent_blocks = unmapped_blocks = unconfirmed_blocks = invalid_blocks = coverage_errors = 0
    rendered_ends: dict[tuple[object, object], int] = {}
    for event in events:
        if event.get("type") != "playback.rendered":
            continue
        active_count = event.get("activeSamples")
        context_id, epoch = event.get("contextId"), event.get("epoch")
        source_start, samples = event.get("sourceStart"), event.get("samples")
        if (
            type(context_id) is not int
            or type(epoch) is not int
            or type(source_start) is not int
            or type(samples) is not int
            or not 0 < samples <= 480
            or source_start < 0
            or type(active_count) is not int
            or not 0 <= active_count <= samples
        ):
            invalid_blocks += 1
            continue
        coverage_key = (context_id, epoch)
        if coverage_key in rendered_ends and source_start != rendered_ends[coverage_key]:
            coverage_errors += 1
        rendered_ends[coverage_key] = source_start + samples
        delivery_ms = number(event.get("monotonicMs"), float("nan"))
        context_rate = number(event.get("contextSampleRate"))
        at_value, until_value = event.get("at"), event.get("until")
        at = number(at_value, float("nan"))
        until = number(until_value, float("nan"))
        expected_span = (samples - 1) / 24000
        time_tolerance = 128 / context_rate + expected_span * 0.001 if context_rate > 0 else 0
        if (
            type(event.get("sampleRate")) is not int
            or event.get("sampleRate") != 24000
            or type(event.get("contextSampleRate")) is not int
            or context_rates.get(context_id) != context_rate
            or not isinstance(at_value, (int, float))
            or isinstance(at_value, bool)
            or not isinstance(until_value, (int, float))
            or isinstance(until_value, bool)
            or not math.isfinite(at)
            or not math.isfinite(until)
            or at < 0
            or until < at
            or abs((until - at) - expected_span) > time_tolerance
        ):
            invalid_blocks += 1
            continue
        if active_count == 0:
            if (
                event.get("activeSpans") != []
                or event.get("firstActiveSample") != -1
                or event.get("lastActiveSample") != -1
            ):
                invalid_blocks += 1
                continue
            silent_blocks += 1
            continue
        active_blocks += 1
        first, last = event.get("firstActiveSample"), event.get("lastActiveSample")
        spans = event.get("activeSpans")
        if (
            type(context_id) is not int
            or type(epoch) is not int
            or not all(type(item) is int for item in (source_start, samples, first, last, active_count))
            or not isinstance(spans, list)
        ):
            invalid_blocks += 1
            continue
        first = cast("int", first)
        last = cast("int", last)
        if (
            samples <= 0
            or samples > 480
            or source_start < 0
            or first < 0
            or last < first
            or last >= samples
            or active_count <= 0
            or active_count > last - first + 1
        ):
            invalid_blocks += 1
            continue
        active_offsets: list[int] = []
        normalized_spans: list[tuple[int, int]] = []
        valid_spans = True
        previous_span_end = first
        for span in spans:
            if (
                not isinstance(span, list)
                or len(span) != 2
                or not all(type(item) is int for item in span)
                or span[0] < first
                or span[0] < previous_span_end
                or span[1] <= span[0]
                or span[1] - 1 > last
                or span[1] > samples
            ):
                valid_spans = False
                break
            span_start, span_end = cast("int", span[0]), cast("int", span[1])
            normalized_spans.append((span_start, span_end))
            active_offsets.extend(range(span_start, span_end))
            previous_span_end = span_end
        if not valid_spans or len(active_offsets) != active_count:
            invalid_blocks += 1
            continue
        if not normalized_spans or normalized_spans[0][0] != first or normalized_spans[-1][1] - 1 != last:
            invalid_blocks += 1
            continue
        clock_tolerance_ms = 128 / context_rate * 1000

        active_windows = [
            (
                _audio_time_monotonic_ms(_source_offset_time(at, until, low, samples), event, delivery_ms),
                _audio_time_monotonic_ms(_source_offset_time(at, until, high - 1, samples), event, delivery_ms),
            )
            for low, high in normalized_spans
        ]
        if not all(math.isfinite(left) and math.isfinite(right) for left, right in active_windows):
            invalid_blocks += 1
            continue
        if not all(has_output(context_id, start, end, clock_tolerance_ms) for start, end in active_windows):
            unconfirmed_blocks += 1
            continue
        submission_key = (context_id, epoch)
        ranges = submissions.get(submission_key, [])
        range_starts = submission_starts.get(submission_key, [])
        if (context_id, epoch) in invalid_submissions:
            invalid_blocks += 1
            continue
        # Sample the active waveform at 1 ms intervals. This preserves useful
        # p95 resolution without retaining one Python float per PCM sample.
        sampled_offsets = active_offsets[::24]
        if active_offsets and sampled_offsets[-1] != active_offsets[-1]:
            sampled_offsets.append(active_offsets[-1])
        for offset in sampled_offsets:
            coordinate = source_start + offset
            index = bisect_right(range_starts, coordinate) - 1
            if index < 0 or not ranges[index][0] <= coordinate < ranges[index][1]:
                unmapped_blocks += 1
                break
            match = ranges[index][2]
            render_ms = _audio_time_monotonic_ms(_source_offset_time(at, until, offset, samples), event, delivery_ms)
            age = render_ms - match
            if not math.isfinite(age) or age < -2:
                invalid_blocks += 1
                break
            age = max(0, age)
            ages.append(age)
            minute = max(
                0,
                int(
                    (
                        _audio_time_monotonic_ms(_source_offset_time(at, until, offset, samples), event, delivery_ms)
                        - beginning
                    )
                    // 60000
                ),
            )
            minute_ages.setdefault(minute, []).append(age)

    available = (
        bool(ages)
        and output_complete
        and context_rate_valid
        and invalid_submission_events == 0
        and invalid_output_contexts == 0
        and not any((unmapped_blocks, unconfirmed_blocks, invalid_blocks, coverage_errors))
    )
    omitted_events = number(report.get("omitted"))
    if omitted_events > 0:
        available = False
    windows = [
        {
            "minute": minute,
            "observations": len(values),
            "p95Ms": percentile(values),
            "maxMs": max(values),
        }
        for minute, values in sorted(minute_ages.items())
    ]
    return {
        "available": available,
        "outputPcmComplete": output_complete,
        "contextSampleRatesValid": context_rate_valid,
        "invalidSubmissionEvents": invalid_submission_events,
        "invalidOutputContexts": invalid_output_contexts,
        "observations": len(ages),
        "observationIntervalMs": 1,
        "p95Ms": percentile(ages) if available else None,
        "maxMs": max(ages, default=0) if available else None,
        "p95Within250Ms": available and percentile(ages) <= 250,
        "peakWithin1000Ms": available and max(ages, default=0) <= 1000,
        "withinBounds": available and percentile(ages) <= 250 and max(ages, default=0) <= 1000,
        "activeBlocks": active_blocks,
        "silentBlocks": silent_blocks,
        "unmappedBlocks": unmapped_blocks,
        "unconfirmedPostVolumeBlocks": unconfirmed_blocks,
        "invalidBlocks": invalid_blocks,
        "coverageErrors": coverage_errors,
        "omittedEvents": omitted_events,
        "minuteWindows": windows,
        "meaning": "Arrival-to-render age for active source PCM only, confirmed by nonzero post-volume output in the same render window; not provider or acoustic latency.",
    }


def timing_metrics(report: dict[str, object]) -> dict[str, object]:
    """Separate rendered speech timing, sampled browser queue age and network gaps."""
    events = objects(report.get("events"))
    health = objects(report.get("health"))
    starts = [event for event in events if event.get("type") == "fixture.start"]
    output = objects(report.get("output"))
    measurement = [number(event.get("monotonicMs")) for event in events if event.get("type") == "measurement.start"]
    beginning = min(measurement, default=min((number(item.get("monotonicMs")) for item in starts), default=0))
    # Connection setup may reset playback before recording is armed. Retain it
    # in the raw trace, but never mistake a warmup epoch for a recording reset.
    health = [item for item in health if number(item.get("monotonicMs")) >= beginning]
    voiced_times: list[float] = []
    for block in output:
        data = pcm_bytes(block)
        if len(data) % 2:
            continue
        samples = array.array("h")
        samples.frombytes(data)
        if sys.byteorder != "little":
            samples.byteswap()
        for offset, sample in enumerate(samples):
            if abs(sample) > 327:
                voiced_times.append(render_monotonic_ms(block, offset))
                break
    voiced_times.sort()
    transmitted_times = sorted(
        number(block.get("monotonicMs")) for block in objects(report.get("input")) if captured_signal([block])
    )
    rounds: list[dict[str, object]] = []
    for index, start in enumerate(starts):
        scheduled_start = number(start.get("monotonicMs")) + number(start.get("delayMs"))
        scheduled_end = scheduled_start + number(start.get("duration")) * 1000
        next_start = number(starts[index + 1].get("monotonicMs")) if index + 1 < len(starts) else float("inf")
        audible_index = bisect_left(voiced_times, scheduled_end)
        heard = audible_index < len(voiced_times) and voiced_times[audible_index] < next_start
        microphone_index = bisect_left(transmitted_times, scheduled_start)
        sent = microphone_index < len(transmitted_times) and transmitted_times[microphone_index] < next_start
        rounds.append(
            {
                "round": start.get("round", index + 1),
                "fixtureStartMs": scheduled_start,
                "fixtureEndMs": scheduled_end,
                "renderedSpeechAfterFixture": heard,
                "microphoneSpeechAfterFixture": sent,
                "firstMicrophoneTransmitAfterFixtureStartMs": transmitted_times[microphone_index] - scheduled_start
                if sent
                else None,
                "firstAudibleAfterFixtureEndMs": voiced_times[audible_index] - scheduled_end if heard else None,
            }
        )
    receives = [event for event in events if event.get("type") == "socket.receive"]
    cumulative = [number(event.get("receivedSamples")) for event in receives]
    # Cumulative counters can only identify the consumed packet when the run
    # has exactly one socket, no dropped samples and no resets/mute epochs.
    mapping_valid = (
        bool(receives)
        and all(left < right for left, right in pairwise(cumulative))
        and len({event.get("path") for event in receives}) == 1
        and len({item.get("epoch") for item in health}) <= 1
        and len({item.get("contextId") for item in health}) <= 1
        and all(number(item.get("droppedSamples")) == 0 for item in health)
        and all(
            number(left.get("playedSamples")) <= number(right.get("playedSamples")) for left, right in pairwise(health)
        )
    )
    windows: dict[int, dict[str, object]] = {}
    for item in health:
        elapsed = number(item.get("monotonicMs")) - beginning
        if elapsed < 0:
            continue
        minute = int(elapsed // 60000)
        window = windows.setdefault(
            minute, {"minute": minute, "bufferedMs": [], "targetMs": [], "sampleAgeMs": [], "health": []}
        )
        for name in ("bufferedMs", "targetMs"):
            cast("list[float]", window[name]).append(number(item.get(name)))
        cast("list[dict[str, object]]", window["health"]).append(item)
        played = number(item.get("playedSamples"))
        consumed = bisect_left(cumulative, played)
        # Empty queues and inter-utterance silence are not accumulating latency.
        if mapping_valid and played > 0 and number(item.get("bufferedMs")) > 0 and consumed < len(receives):
            rendered = number(item.get("monotonicMs"))
            if "renderedAt" in item:
                timestamp = item.get("outputTimestamp")
                if isinstance(timestamp, dict) and number(timestamp.get("performanceTime")) > 0:
                    rendered = (
                        number(timestamp.get("performanceTime"))
                        + (number(item["renderedAt"]) - number(timestamp.get("contextTime"))) * 1000
                    )
                else:
                    rendered += (number(item["renderedAt"]) - number(item.get("audioTime"))) * 1000
            age = rendered - number(receives[consumed].get("monotonicMs"))
            cast("list[float]", window["sampleAgeMs"]).append(max(0, age))
    summaries: list[dict[str, object]] = []
    previous_underruns = previous_drops = 0.0
    for minute, window in sorted(windows.items()):
        observations = cast("list[dict[str, object]]", window["health"])
        last = observations[-1]
        underruns, drops = number(last.get("underruns")), number(last.get("droppedSamples"))
        values: dict[str, object] = {
            "minute": minute,
            "healthSamples": len(observations),
            "underruns": max(0, underruns - previous_underruns),
            "droppedSamples": max(0, drops - previous_drops),
        }
        previous_underruns, previous_drops = underruns, drops
        for name in ("bufferedMs", "targetMs", "sampleAgeMs"):
            observations_ms = cast("list[float]", window[name])
            values[name] = {
                "samples": len(observations_ms),
                "p95": percentile(observations_ms),
                "max": max(observations_ms, default=0),
            }
        packet_gaps = [
            number(right.get("monotonicMs")) - number(left.get("monotonicMs"))
            for left, right in pairwise(receives)
            if int((number(right.get("monotonicMs")) - beginning) // 60000) == minute
        ]
        values["receiveIntervalsIncludingNaturalPausesMs"] = {
            "p95": percentile(packet_gaps),
            "max": max(packet_gaps, default=0),
        }
        summaries.append(values)
    lengths = []
    for context_id in {number(block.get("contextId")) for block in output}:
        blocks = [block for block in output if number(block.get("contextId")) == context_id]
        rate = number(blocks[0].get("sampleRate"))
        if rate > 0:
            lengths.append(
                (number(blocks[-1].get("frame")) + number(blocks[-1].get("samples")) - number(blocks[0].get("frame")))
                / rate
            )
    return {
        "recordedDurationSeconds": max(lengths, default=0),
        "fixtureSpanSeconds": (number(starts[-1].get("monotonicMs")) - number(starts[0].get("monotonicMs"))) / 1000
        if starts
        else 0,
        "rounds": rounds,
        "minuteWindows": summaries,
        "sampleAgeAvailable": mapping_valid,
        "sampleAgeMeaning": "Received packet to audio-thread renderedAt (when present), otherwise health callback, while audio is queued. Callback fallback includes scheduling delay. Not provider or acoustic latency.",
        "renderedSpeechAge": rendered_speech_age(report),
        "graphRenderedSpeechAge": graph_rendered_speech_age(report),
        "audioThreadHealthClockPresent": bool(health) and all("renderedAt" in item for item in health),
        "replyTimingMeaning": "First rendered non-silent sample after the known utterance ends; speech is observed, not causally assigned to a delegation.",
    }


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
        if record.get("schema") == "ngn-browser-voice-e2e/v2":
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
    _, received_metrics = unpack_received_pcm(report, directory)
    metrics["receivedPcm"] = received_metrics
    outputs: list[dict[str, object]] = []
    gaps = recorder_gaps(report)
    events = objects(report.get("events"))
    for context_id in context_ids:
        # Preserve delivery order even when the render-clock observations are
        # invalid. Sorting by a faulty clock can itself reorder audible samples.
        blocks = [item for item in captures if number(item.get("contextId")) == context_id]
        rate = int(number(blocks[0].get("sampleRate")))
        result = b"".join(pcm_bytes(block) for block in blocks)
        raw_filename = f"speaker-callback-order-{context_id}.wav"
        write_wave(directory / raw_filename, result, rate)
        context_gaps = [gap for gap in gaps if number(gap.get("contextId")) == context_id]
        jumps = [
            event
            for event in events
            if event.get("type") == "recorder.clock-discontinuity"
            and event.get("phase") == "recording"
            and number(event.get("contextId")) == context_id
        ]
        continuous = complete_pcm(blocks, output=True) and recorder_counters_continuous(blocks) and not context_gaps
        filename = f"speaker-rendered-{context_id}.wav" if continuous else ""
        if filename:
            write_wave(directory / filename, result, rate)
        outputs.append(
            {
                "file": filename,
                "callbackOrderFile": raw_filename,
                "recorderContinuous": continuous,
                "recordingClockDiscontinuities": len(jumps),
                "sampleRate": rate,
                "firstAudioFrame": int(number(blocks[0].get("frame"))),
                "samples": len(result) // 2,
                "missingRecorderFrames": sum(max(0, number(gap["missingFrames"])) for gap in context_gaps),
                "silenceCandidates": silence_spans(result, rate),
            }
        )
    metrics["outputs"] = outputs
    health = objects(report.get("health"))
    metrics["lastPlaybackHealth"] = health[-1] if health else {}
    metrics["mainThreadGaps"] = [event for event in events if event.get("type") == "main-thread.gap"]
    errors = [event for event in events if "error" in str(event.get("type", "")) or event.get("eventType") == "error"]
    metrics["errors"] = errors
    intervals: dict[str, object] = {}
    for direction in ("send", "receive"):
        times = [number(event.get("monotonicMs")) for event in events if event.get("type") == f"socket.{direction}"]
        intervals_ms = sorted(right - left for left, right in pairwise(times))
        intervals[direction] = {
            "frames": len(times),
            "maxMs": max(intervals_ms, default=0),
            "p95Ms": intervals_ms[min(len(intervals_ms) - 1, int(len(intervals_ms) * 0.95))] if intervals_ms else 0,
        }
    metrics["socketIntervalsIncludingNaturalPauses"] = intervals
    metrics["timing"] = timing_metrics(report)
    metrics["checks"] = {
        "captureStopped": report.get("stopped") is True,
        "transmittedSpeech": any(number(item.get("rms")) > 0.01 for item in inputs),
        "renderedSpeech": any(number(item.get("rms")) > 0.01 for item in captures),
        "recorderContinuous": bool(outputs) and all(item["recorderContinuous"] for item in outputs),
        "observedDelegationCompletion": any(
            event.get("eventType") == "delegation" and event.get("status") == "completed" for event in events
        ),
        "noObservedErrors": not errors,
    }
    # Keep timestamps and block statistics, remove bulk audio from readable trace.
    for channel in ("input", "output", "received"):
        for block in objects(report.get(channel)):
            block.pop("pcm16", None)
    (directory / "trace.json").write_text(json.dumps(report, indent=2) + "\n")
    (directory / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    print(json.dumps(metrics, indent=2))


def unpack_received_pcm(report: dict[str, object], directory: Path) -> tuple[bytes, dict[str, object]]:
    """Write received network PCM only when its complete packet ledger validates.

    Older v2 captures did not collect this channel. They are explicitly marked
    not-captured; partial or inconsistent new captures never produce a WAV.
    """
    filename = directory / "speaker-received.wav"
    if filename.is_symlink() or (filename.exists() and not filename.is_file()):
        raise SystemExit("speaker-received.wav must be a regular generated artifact")
    packets_value = report.get("received")
    summary_value = report.get("receivedPcm")
    if packets_value is None and summary_value is None:
        filename.unlink(missing_ok=True)
        return b"", {"status": "not-captured", "complete": False, "file": ""}

    summary = summary_value if isinstance(summary_value, dict) else {}
    packets = objects(packets_value)
    expected_keys = (
        "sampleFormat",
        "sampleRate",
        "packetCount",
        "capturedPackets",
        "samples",
        "capturedSamples",
        "capturedBytes",
        "omittedPackets",
        "omittedBytes",
    )
    integer_summary_keys = (
        "sampleRate",
        "packetCount",
        "capturedPackets",
        "samples",
        "capturedSamples",
        "capturedBytes",
        "omittedPackets",
        "omittedBytes",
    )
    valid = (
        isinstance(packets_value, list)
        and summary.get("sampleFormat") == "s16le"
        and all(type(summary.get(key)) is int for key in integer_summary_keys)
        and summary.get("sampleRate") == 24000
        and all(key in summary for key in expected_keys)
        and number(summary.get("omittedPackets")) == 0
        and number(summary.get("omittedBytes")) == 0
        and type(report.get("omitted")) is int
        and report.get("omitted") == 0
        and 0 < len(packets) <= 100_000
    )
    decoded: list[bytes] = []
    total_samples = 0
    total_bytes = 0
    context_id: int | None = None
    previous_order = -1
    for index, packet in enumerate(packets):
        try:
            encoded = packet.get("pcm16")
            if not isinstance(encoded, str):
                raise ValueError("missing pcm16")
            if len(encoded) > 64_000:
                raise ValueError("encoded packet exceeds bound")
            data = base64.b64decode(encoded, validate=True)
            samples = number(packet.get("samples"))
            context_value = packet.get("contextId")
            capture_order = packet.get("captureOrder")
            epoch_value = packet.get("epoch")
            audio_time = packet.get("audioTime")
            if not isinstance(context_value, int) or isinstance(context_value, bool):
                raise ValueError("invalid context ID")
            if not isinstance(capture_order, int) or isinstance(capture_order, bool):
                raise ValueError("invalid capture order")
            if not isinstance(epoch_value, int) or isinstance(epoch_value, bool):
                raise ValueError("invalid epoch")
            if (
                isinstance(audio_time, bool)
                or not isinstance(audio_time, (int, float))
                or not math.isfinite(audio_time)
            ):
                raise ValueError("invalid audio time")
            if (
                len(data) == 0
                or len(data) > 48_000
                or len(data) % 2
                or samples <= 0
                or samples % 1
                or any(
                    type(packet.get(key)) is not int
                    for key in ("sequence", "sampleOffset", "samples", "bytes", "contextId", "captureOrder", "epoch")
                )
                or packet.get("sequence") != index
                or type(packet.get("receivedSamples")) is not int
                or packet.get("receivedSamples") != total_samples + samples
                or len(data) != samples * 2
                or number(packet.get("bytes")) != len(data)
                or number(packet.get("sampleRate")) != 24000
                or number(packet.get("sequence")) != index
                or number(packet.get("sequence")) % 1
                or number(packet.get("sampleOffset")) != total_samples
                or number(packet.get("sampleOffset")) % 1
                or context_value < 0
                or capture_order <= previous_order
                or not isinstance(packet.get("pcm16"), str)
            ):
                raise ValueError("invalid packet ledger")
            if context_id is None:
                context_id = context_value
            elif context_value != context_id:
                raise ValueError("mixed playback contexts")
        except (ValueError, TypeError):
            valid = False
            break
        previous_order = capture_order
        total_samples += len(data) // 2
        total_bytes += len(data)
        if total_bytes > 30_000_000:
            valid = False
            break
        decoded.append(data)

    valid = valid and (
        len(decoded) == len(packets)
        and number(summary.get("packetCount")) == len(packets)
        and number(summary.get("capturedPackets")) == len(packets)
        and number(summary.get("samples")) == total_samples
        and number(summary.get("capturedSamples")) == total_samples
        and number(summary.get("capturedBytes")) == total_bytes
    )
    if not valid:
        filename.unlink(missing_ok=True)
        return b"", {
            "status": "incomplete",
            "complete": False,
            "file": "",
            "capturedPackets": len(decoded),
            "reportedPackets": number(summary.get("capturedPackets")),
            "omittedPackets": number(summary.get("omittedPackets")),
            "omittedBytes": number(summary.get("omittedBytes")),
        }

    audio = b"".join(decoded)
    filename_name = filename.name
    write_wave(filename, audio, 24000)
    filename.chmod(0o600)
    return audio, {
        "status": "complete",
        "complete": True,
        "file": filename_name,
        "sampleRate": 24000,
        "packets": len(packets),
        "samples": total_samples,
        "bytes": total_bytes,
    }


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
        ordered = [block for block in blocks if number(block.get("contextId")) == context_id]
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
            for key in ("contextId", "frame", "observedFrame", "sampleOffset", "sequence"):
                value = block.get(key)
                if (
                    type(value) not in {int, float}
                    or not math.isfinite(number(value))
                    or number(value) < 0
                    or not number(value).is_integer()
                ):
                    return False
    return True


def recorder_counters_continuous(blocks: list[dict[str, object]]) -> bool:
    """Check message order and every processed sample independently of raw clock labels."""
    if not complete_pcm(blocks, output=True):
        return False
    for context_id in {number(block.get("contextId")) for block in blocks}:
        ordered = [block for block in blocks if number(block.get("contextId")) == context_id]
        origin, rate = number(ordered[0].get("frame")), number(ordered[0].get("sampleRate"))
        offset = 0.0
        for sequence, block in enumerate(ordered):
            if (
                number(block.get("sequence")) != sequence
                or number(block.get("sampleOffset")) != offset
                or number(block.get("frame")) != origin + offset
                or number(block.get("sampleRate")) != rate
            ):
                return False
            offset += number(block.get("samples"))
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
    report: dict[str, object],
    *,
    mode: str,
    reply_samples: int = 0,
    expect_gap_ms: int = 0,
    expect_text: str = "",
    min_rounds: int = 0,
    min_active_span_seconds: float = 0,
    max_buffered_ms: float = 0,
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
    context_samples: dict[float, float] = {}
    context_blocks: dict[float, int] = {}
    for block in outputs:
        context_id = number(block.get("contextId"))
        first_frames.setdefault(context_id, number(block.get("frame")))
        final_frames[context_id] = number(block.get("frame")) + number(block.get("samples"))
        context_samples[context_id] = context_samples.get(context_id, 0) + number(block.get("samples"))
        context_blocks[context_id] = context_blocks.get(context_id, 0) + 1
    checks = {
        "expectedSchema": report.get("schema") == "ngn-browser-voice-e2e/v2",
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
                number(start.get("contextId")) == context_id
                and number(start.get("frame")) == frame
                and start.get("sampleOffset") == 0
                and start.get("sequence") == 0
                for start in starts
            )
            for context_id, frame in first_frames.items()
        ),
        "measurementWindowStopped": bool(final_frames)
        and all(
            any(
                number(stop.get("contextId")) == context_id
                and number(stop.get("finalFrame")) == frame
                and stop.get("samplesCaptured") == context_samples[context_id]
                and stop.get("blocksCaptured") == context_blocks[context_id]
                for stop in stops
            )
            for context_id, frame in final_frames.items()
        ),
        "recorderContinuity": recorder_counters_continuous(outputs) and not gaps,
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
        "renderClockConsistent": not recording_jumps,
        "clockMeaning": "frame follows actual processed sample counts; observedFrame retains the browser's raw currentFrame label. Raw clock anomalies are diagnostics, not evidence of missing PCM. Physical device timing is not measured.",
    }
    timing = timing_metrics(report)
    details["timing"] = timing
    rounds = objects(timing["rounds"])
    if min_rounds:
        checks["enoughSpokenRounds"] = len(rounds) >= min_rounds and all(
            item["renderedSpeechAfterFixture"] and item["microphoneSpeechAfterFixture"] for item in rounds
        )
    if min_active_span_seconds:
        checks["longActiveSession"] = (
            number(timing["recordedDurationSeconds"]) >= min_active_span_seconds
            and number(timing["fixtureSpanSeconds"]) >= min_active_span_seconds
            and len(rounds) >= 2
            and all(item["renderedSpeechAfterFixture"] and item["microphoneSpeechAfterFixture"] for item in rounds)
        )
    if max_buffered_ms:
        checks["boundedBrowserPlaybackQueue"] = bool(health) and all(
            max(number(item.get("bufferedMs")), number(item.get("peakBufferedMs"))) <= max_buffered_ms
            for item in health
        )
    if expect_text:
        spoken = " ".join(
            str(event.get("text", ""))
            for event in events
            if event.get("eventType") == "transcript" and event.get("speaker") == "assistant"
        )
        checks["expectedSpokenText"] = " ".join(expect_text.casefold().split()) in " ".join(spoken.casefold().split())
    if mode == "authenticated":
        lifecycles: dict[str, set[str]] = {}
        task_lifecycles: dict[str, set[str]] = {}
        for event in events:
            if event.get("eventType") == "delegation":
                identifier = str(event.get("delegation_id", ""))
                lifecycles.setdefault(identifier, set()).add(str(event.get("status", "")))
            if event.get("eventType") in {"task_started", "task_completed"}:
                task_id = str(event.get("task_id", ""))
                run_id = str(event.get("run_id", ""))
                if task_id and run_id:
                    event_type = str(event.get("eventType", ""))
                    task_state = (
                        ("task_completed" if event.get("status") == "completed" else "task_failed")
                        if event_type == "task_completed"
                        else event_type
                    )
                    task_lifecycles.setdefault(f"{run_id}:{task_id}", set()).add(task_state)
        direct_delegation = any(
            identifier and {"queued", "working", "completed"} <= statuses for identifier, statuses in lifecycles.items()
        )
        completed_task = any(
            {"task_started", "task_completed"} <= statuses and "task_failed" not in statuses
            for statuses in task_lifecycles.values()
        )
        checks["realDelegationCompleted"] = direct_delegation or completed_task
        checks["providerFinalized"] = any(
            "Finalization confirmed." in str(event.get("text", "")) + str(event.get("message", "")) for event in events
        )
        details["delegationStatuses"] = {key: sorted(value) for key, value in lifecycles.items()}
        details["delegatedTaskLifecycles"] = {key: sorted(value) for key, value in task_lifecycles.items()}
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


def check_authenticated_scenario(
    report: dict[str, object],
    rounds: list[dict[str, object]],
    *,
    minimum_active_span_seconds: float = 0,
    max_rendered_speech_p95_ms: float = 0,
    max_rendered_speech_peak_ms: float = 0,
    require_rendered_speech_telemetry: bool = False,
) -> dict[str, object]:
    """Require each real spoken prompt to receive its expected answer and work lifecycle."""
    base = check_capture(
        report,
        mode="authenticated",
        min_rounds=len(rounds),
        min_active_span_seconds=minimum_active_span_seconds,
    )
    checks = cast("dict[str, bool]", base["checks"])
    events = objects(report.get("events"))
    timing = timing_metrics(report)
    timing_rounds = {int(number(item.get("round"))): item for item in objects(timing.get("rounds"))}
    starts = {
        int(number(event.get("round"))): number(event.get("monotonicMs"))
        for event in events
        if event.get("type") == "fixture.start"
    }
    round_details: list[dict[str, object]] = []
    load_latencies: dict[str, list[float]] = {}
    for index, scenario_round in enumerate(rounds, start=1):
        start_ms = starts.get(index)
        next_start = min((value for key, value in starts.items() if key > index), default=float("inf"))
        segment = [
            event
            for event in events
            if start_ms is not None and start_ms <= number(event.get("monotonicMs")) < next_start
        ]
        transcripts = [event for event in segment if event.get("eventType") == "transcript"]
        user_text = " ".join(str(event.get("text", "")) for event in transcripts if event.get("speaker") == "user")
        assistant_text = " ".join(
            str(event.get("text", "")) for event in transcripts if event.get("speaker") == "assistant"
        )
        expected = expected_phrases(scenario_round.get("expect_text"))
        normalized_response = normalized_text(assistant_text)
        matched_expected = next((phrase for phrase in expected if normalized_text(phrase) in normalized_response), "")
        phrase_present = bool(matched_expected)
        round_timing = timing_rounds.get(index, {})
        round_audio = bool(round_timing.get("microphoneSpeechAfterFixture")) and bool(
            round_timing.get("renderedSpeechAfterFixture")
        )
        lifecycles: dict[str, set[str]] = {}
        task_lifecycles: dict[str, set[str]] = {}
        for event in segment:
            if event.get("eventType") == "delegation":
                identifier = str(event.get("delegation_id", ""))
                if identifier:
                    lifecycles.setdefault(identifier, set()).add(str(event.get("status", "")))
            if event.get("eventType") in {"task_started", "task_completed"}:
                task_id = str(event.get("task_id", ""))
                run_id = str(event.get("run_id", ""))
                if task_id and run_id:
                    event_type = str(event.get("eventType", ""))
                    status = str(event.get("status", ""))
                    task_state = (
                        ("task_completed" if status == "completed" else "task_failed")
                        if event_type == "task_completed"
                        else event_type
                    )
                    task_lifecycles.setdefault(f"{run_id}:{task_id}", set()).add(task_state)
        requires_delegation = scenario_round.get("require_delegation") is True
        direct_delegation_complete = any(
            {"queued", "working", "completed"} <= statuses for statuses in lifecycles.values()
        )
        round_ok = start_ms is not None and bool(user_text.strip()) and phrase_present and round_audio
        if requires_delegation:
            round_ok = round_ok and direct_delegation_complete
        requires_subagent = scenario_round.get("require_subagent") is True
        subagent_complete = any(
            {"task_started", "task_completed"} <= statuses and "task_failed" not in statuses
            for statuses in task_lifecycles.values()
        )
        if requires_subagent:
            round_ok = round_ok and subagent_complete
        pending_tasks = [
            key
            for key, statuses in task_lifecycles.items()
            if "task_started" in statuses and "task_completed" not in statuses
        ]
        failed_tasks = [key for key, statuses in task_lifecycles.items() if "task_failed" in statuses]
        round_ok = round_ok and not pending_tasks and not failed_tasks
        requires_tool = scenario_round.get("require_tool_call") is True
        tool_events = [
            event
            for event in segment
            if event.get("eventType") in {"tool_call", "tool_execution_started", "tool_result"}
        ]
        tool_lifecycles: dict[str, set[str]] = {}
        for event in tool_events:
            call_id = str(event.get("call_id") or event.get("id") or "")
            run_id = str(event.get("run_id", ""))
            if call_id:
                tool_lifecycles.setdefault(f"{run_id}:{call_id}", set()).add(str(event.get("eventType", "")))
        tool_completed = any(
            {"tool_call", "tool_execution_started", "tool_result"} <= statuses for statuses in tool_lifecycles.values()
        )
        if requires_tool:
            round_ok = round_ok and tool_completed
        checks[f"round{index}ExpectedResponse"] = round_ok
        load = str(scenario_round.get("load", "baseline"))[:80]
        latency = number(round_timing.get("firstAudibleAfterFixtureEndMs"), -1)
        if latency >= 0:
            load_latencies.setdefault(load, []).append(latency)
        round_details.append(
            {
                "round": index,
                "name": str(scenario_round.get("name", f"round-{index}"))[:100],
                "load": load,
                "userCaptionObserved": bool(user_text.strip()),
                "expectedTexts": expected,
                "matchedExpectedText": matched_expected,
                "assistantText": assistant_text,
                "expectedTextObserved": phrase_present,
                "spokenPcmObserved": round_audio,
                "requireDelegation": requires_delegation,
                "requireSubagent": requires_subagent,
                "delegationStatuses": {key: sorted(value) for key, value in lifecycles.items()},
                "taskLifecycles": {key: sorted(value) for key, value in task_lifecycles.items()},
                "pendingTasks": pending_tasks,
                "failedTasks": failed_tasks,
                "delegationCompleted": direct_delegation_complete if requires_delegation else None,
                "subagentCompleted": subagent_complete if requires_subagent else None,
                "requireToolCall": requires_tool,
                "toolCallProgress": {key: sorted(value) for key, value in tool_lifecycles.items()},
                "toolCallCompleted": tool_completed if requires_tool else None,
                "firstAudibleAfterFixtureEndMs": latency if latency >= 0 else None,
            }
        )

    health = objects(report.get("health"))
    outputs = objects(report.get("output"))
    classified_underruns: list[dict[str, object]] = []
    unclassified_underruns: list[dict[str, object]] = []
    ambiguous_queue_empty_underruns: list[dict[str, object]] = []
    starvation_during_audio: list[dict[str, object]] = []
    previous_underruns = 0.0
    previous_health_ms: float | None = None
    for sample in health:
        underruns = number(sample.get("underruns"))
        received = number(sample.get("receivedSamples"))
        played = number(sample.get("playedSamples"))
        buffered = number(sample.get("bufferedMs"))
        current_ms = number(sample.get("monotonicMs"), -1)
        change = max(0, underruns - previous_underruns)
        if change:
            item: dict[str, object] = {
                "monotonicMs": current_ms if current_ms >= 0 else None,
                "underruns": change,
                "queuedSamples": max(0, received - played),
                "bufferedMs": buffered,
                "lastUnderrunAtAudioSeconds": number(sample.get("lastUnderrunAt"), -1),
            }
            if received <= played and buffered <= 0:
                item["classification"] = (
                    "ambiguous queue-empty pause: provider DTX/natural pause and starvation cannot be distinguished"
                )
                ambiguous_queue_empty_underruns.append(item)
            elif current_ms < 0 or previous_health_ms is None or number(sample.get("lastUnderrunAt"), -1) < 0:
                item["classification"] = "unclassifiable: missing monotonic health interval"
                unclassified_underruns.append(item)
            else:
                output_timestamp = sample.get("outputTimestamp")
                underrun_ms = -1.0
                if isinstance(output_timestamp, dict) and number(output_timestamp.get("performanceTime")) > 0:
                    underrun_ms = (
                        number(output_timestamp.get("performanceTime"))
                        + (number(sample.get("lastUnderrunAt")) - number(output_timestamp.get("contextTime"))) * 1000
                    )
                active_audio = False
                for block in outputs:
                    pcm = pcm_bytes(block)
                    rate = number(block.get("sampleRate"))
                    if not pcm or rate <= 0 or underrun_ms < 0:
                        continue
                    samples = array.array("h")
                    samples.frombytes(pcm)
                    if sys.byteorder != "little":
                        samples.byteswap()
                    frame_start_ms = render_monotonic_ms(block)
                    start_sample = max(0, int((underrun_ms - frame_start_ms - 25) * rate / 1000))
                    end_sample = min(len(samples), int((underrun_ms - frame_start_ms + 25) * rate / 1000))
                    active_audio = any(abs(sample_value) > 327 for sample_value in samples[start_sample:end_sample])
                    if active_audio:
                        break
                if active_audio:
                    item["monotonicMs"] = underrun_ms
                    item["classification"] = "starvation counter advanced during captured non-silent output PCM"
                    starvation_during_audio.append(item)
                else:
                    item["monotonicMs"] = underrun_ms if underrun_ms >= 0 else None
                    item["classification"] = "unclassifiable: queued audio underrun did not overlap captured speech PCM"
                    unclassified_underruns.append(item)
        previous_underruns = max(previous_underruns, underruns)
        if current_ms >= 0:
            previous_health_ms = current_ms
    speech_age = cast("dict[str, object]", timing.get("graphRenderedSpeechAge", {}))
    trace_verifies_voice_continuity = (
        require_rendered_speech_telemetry
        and speech_age.get("available") is True
        and speech_age.get("voiceContinuityVerified") is True
    )
    checks["noClassifiedStarvationDuringSpeech"] = not starvation_during_audio or trace_verifies_voice_continuity
    checks["noUnclassifiedQueuedUnderruns"] = not unclassified_underruns or trace_verifies_voice_continuity
    if require_rendered_speech_telemetry:
        speech_windows = objects(speech_age.get("minuteWindows"))
        has_speech_samples = any(number(window.get("observations")) > 0 for window in speech_windows)
        checks["renderedSpeechTelemetryAvailable"] = speech_age.get("available") is True and has_speech_samples
        checks["renderedSpeechGapWithinBound"] = speech_age.get("voiceContinuityVerified") is True
        if max_rendered_speech_p95_ms > 0:
            p95_values = [number(window.get("p95Ms"), float("inf")) for window in speech_windows]
            checks["renderedSpeechP95WithinBound"] = (
                bool(p95_values) and has_speech_samples and max(p95_values) <= max_rendered_speech_p95_ms
            )
        if max_rendered_speech_peak_ms > 0:
            peak_values = [number(window.get("maxMs"), float("inf")) for window in speech_windows]
            checks["renderedSpeechPeakWithinBound"] = (
                bool(peak_values) and has_speech_samples and max(peak_values) <= max_rendered_speech_peak_ms
            )
    trace_gates_passed = (
        trace_verifies_voice_continuity
        and checks.get("renderedSpeechP95WithinBound", True)
        and checks.get("renderedSpeechPeakWithinBound", True)
    )
    gaps = [number(event.get("gapMs")) for event in events if event.get("type") == "main-thread.gap"]
    details = cast("dict[str, object]", base["details"])
    details.update(
        {
            "scenarioRounds": round_details,
            "latencyMsByLoad": {
                name: {"samples": len(values), "p95": percentile(values), "max": max(values)}
                for name, values in sorted(load_latencies.items())
            },
            "mainThreadGapsOver80Ms": {
                "count": len(gaps),
                "p95Ms": percentile(gaps),
                "maxMs": max(gaps, default=0),
            },
            "ambiguousQueueEmptyUnderruns": ambiguous_queue_empty_underruns,
            "classifiedQueueEmptyUnderruns": classified_underruns,
            "unclassifiedUnderruns": unclassified_underruns,
            "starvationDuringRenderedSpeech": starvation_during_audio,
            "counterClassificationsOverriddenByCompleteGraphTrace": trace_verifies_voice_continuity,
            "smoothnessConclusion": (
                "A complete source-to-output trace verified short-pause voice continuity within the configured "
                "latency bounds; counter-only starvation classifications were retained as diagnostics. Long input "
                "gaps without source-silence samples remain unclassifiable."
                if trace_gates_passed
                else "A complete source-to-output trace was available, but its configured continuity or latency "
                "bounds did not pass."
                if speech_age.get("available") is True
                else "Source-to-output voice continuity was not verified; counter-only starvation classifications "
                "remain gating and queue-empty underruns can be ambiguous between provider DTX and starvation."
            ),
            "receiveIntervalMeaning": "Network intervals include provider pacing and natural pauses; they are reported, not treated as jitter by themselves.",
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
    prepare_parser.add_argument(
        "--max-seconds", type=int, default=120, help="Capture window cap after arming, 30 to 900 seconds"
    )
    export_parser = commands.add_parser("unpack")
    export_parser.add_argument("source", type=Path)
    export_parser.add_argument("directory", type=Path)
    check_parser = commands.add_parser("check", help="Exit 1 when the observed E2E contract fails")
    check_parser.add_argument("source", type=Path)
    check_parser.add_argument("--mode", choices=("offline", "authenticated"), required=True)
    check_parser.add_argument("--reply-wav", type=Path)
    check_parser.add_argument("--expect-gap-ms", type=int, default=0)
    check_parser.add_argument("--expect-text", default="")
    check_parser.add_argument(
        "--min-rounds", type=int, default=0, help="Require this many spoken fixtures with audible output after each"
    )
    check_parser.add_argument(
        "--min-active-span-seconds",
        type=float,
        default=0,
        help="Minimum time between first and last spoken fixtures, with real PCM capture",
    )
    check_parser.add_argument(
        "--max-buffered-ms", type=float, default=0, help="Fail when sampled browser playout queue exceeds this bound"
    )
    args = parser.parse_args()
    if args.command == "prepare":
        if not 30 <= args.max_seconds <= 900:
            parser.error("--max-seconds must be between 30 and 900")
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
            min_rounds=args.min_rounds,
            min_active_span_seconds=args.min_active_span_seconds,
            max_buffered_ms=args.max_buffered_ms,
        )
        print(json.dumps(result, indent=2))
        raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
