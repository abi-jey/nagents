#!/usr/bin/env node
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { runInNewContext } from "node:vm";

const PCM_RATE = 24_000;
const DEFAULT_OUTPUT_RATE = 48_000;
const QUANTUM_FRAMES = 128;
const MAX_PACKETS = 100_000;
const MAX_RECEIVED_BYTES = 30_000_000;
const MAX_HEALTH_ROWS = 5_000;

function finite(value) { return typeof value === "number" && Number.isFinite(value); }
function decodePacket(packet) {
  if (typeof packet.pcm16 !== "string") throw new Error("Received PCM packet is missing encoded samples");
  const buffer = Buffer.from(packet.pcm16, "base64");
  if (buffer.toString("base64") !== packet.pcm16) throw new Error("Received PCM packet has invalid base64");
  if (!Number.isSafeInteger(packet.samples) || packet.samples <= 0 || buffer.length !== packet.bytes ||
      buffer.length !== packet.samples * 2 || buffer.length > 48_000 || buffer.length % 2)
    throw new Error("Received PCM packet length is inconsistent or exceeds the playback worklet bound");
  if (packet.sampleRate !== PCM_RATE || !finite(packet.audioTime) || packet.audioTime < 0 ||
      !Number.isSafeInteger(packet.sequence) || !Number.isSafeInteger(packet.captureOrder) ||
      !Number.isSafeInteger(packet.sampleOffset) || !Number.isSafeInteger(packet.receivedSamples) ||
      !Number.isSafeInteger(packet.transportSequence) || !Number.isSafeInteger(packet.transportReceivedSamples) ||
      !Number.isSafeInteger(packet.contextId) || packet.contextId < 0 || !Number.isSafeInteger(packet.epoch))
    throw new Error("Received PCM packet clock/order metadata is invalid");
  return Uint8Array.from(buffer).buffer;
}

function validateCapture(capture) {
  if (capture?.schema !== "ngn-browser-voice-e2e/v2" || !Array.isArray(capture.received) ||
      !Array.isArray(capture.events) || !Array.isArray(capture.contexts) ||
      !Array.isArray(capture.health) || capture.health.length > MAX_HEALTH_ROWS)
    throw new Error("Expected a v2 browser capture with received network PCM");
  const receipt = capture.receivedPcm;
  if (!receipt || receipt.sampleFormat !== "s16le" || receipt.sampleRate !== PCM_RATE)
    throw new Error("Capture does not declare mono s16le/24 kHz source PCM");
  if (receipt.omittedPackets !== 0 || receipt.omittedBytes !== 0 || capture.omitted !== 0)
    throw new Error("Capture reports omitted PCM/events; exact replay is unavailable");
  if (!capture.received.length) throw new Error("Capture contains no received PCM packets");
  if (capture.received.length > MAX_PACKETS || !Number.isSafeInteger(receipt.capturedBytes) ||
      receipt.capturedBytes <= 0 || receipt.capturedBytes > MAX_RECEIVED_BYTES)
    throw new Error("Capture exceeds the offline replay bound");
  if (!Number.isSafeInteger(receipt.capturedPackets) || receipt.capturedPackets !== capture.received.length ||
      !Number.isSafeInteger(receipt.packetCount) || receipt.packetCount !== capture.received.length ||
      !Number.isSafeInteger(receipt.capturedSamples) || !Number.isSafeInteger(receipt.samples))
    throw new Error("Received PCM summary has invalid packet/sample totals");
  const contextIds = new Set(capture.received.map(packet => packet.contextId));
  if (contextIds.size !== 1) throw new Error("Received PCM mixes or lacks a single playback context");
  const contextId = capture.received[0]?.contextId;
  if (!Number.isSafeInteger(contextId) || contextId < 0) throw new Error("Received PCM has an invalid playback context");
  const controls = capture.events.filter(event => event.type === "playback.control" && event.contextId === contextId);
  const submissions = capture.events.filter(event => event.type === "playback.submit" && event.contextId === contextId);
  const orders = [...capture.received.map(packet => packet.captureOrder), ...controls.map(event => event.captureOrder),
    ...submissions.map(event => event.captureOrder)];
  if (orders.some(order => !Number.isSafeInteger(order) || order < 0) || new Set(orders).size !== orders.length)
    throw new Error("Capture has invalid or duplicate ordering metadata");
  if (controls.some(event => !finite(event.audioTime) || !Number.isSafeInteger(event.epoch) ||
      !["mode", "reset", "barrier"].includes(event.controlType)))
    throw new Error("Playback control ledger is invalid");
  if (submissions.some(event => !finite(event.audioTime) || !Number.isSafeInteger(event.receivedSequence) ||
      !Number.isSafeInteger(event.samples) || event.samples <= 0 || event.bytes !== event.samples * 2 ||
      !Number.isSafeInteger(event.submittedSamples) || !Number.isSafeInteger(event.epoch)))
    throw new Error("Playback submission ledger is invalid");
  const submissionsByReceiveSequence = new Map(submissions.map(event => [event.receivedSequence, event]));
  if (submissionsByReceiveSequence.size !== submissions.length)
    throw new Error("Playback submission ledger repeats a receive sequence");
  let expectedOffset = 0;
  let previousSequence = -1;
  let previousCaptureOrder = -1;
  let previousTransportSequence = -1;
  for (const packet of capture.received) {
    decodePacket(packet);
    if (packet.contextId !== contextId || packet.sequence !== previousSequence + 1 ||
        packet.sampleOffset !== expectedOffset || packet.receivedSamples !== expectedOffset + packet.samples ||
        packet.captureOrder <= previousCaptureOrder || !Number.isSafeInteger(packet.transportSequence) ||
        packet.transportSequence <= previousTransportSequence || !Number.isSafeInteger(packet.transportReceivedSamples))
      throw new Error("Received PCM sequence has a missing or reordered packet");
    const submitted = submissionsByReceiveSequence.get(packet.transportSequence);
    if (!submitted || submitted.samples !== packet.samples || submitted.bytes !== packet.bytes ||
        submitted.receivedSamples !== packet.receivedSamples ||
        submitted.transportReceivedSamples !== packet.transportReceivedSamples || submitted.epoch !== packet.epoch)
      throw new Error("Received PCM packet lacks a matching playback submission");
    previousSequence = packet.sequence;
    previousCaptureOrder = packet.captureOrder;
    previousTransportSequence = packet.transportSequence;
    expectedOffset += packet.samples;
  }
  if (expectedOffset !== receipt.capturedSamples || expectedOffset !== receipt.samples ||
      receipt.capturedBytes !== expectedOffset * 2 || capture.received.length !== receipt.packetCount)
    throw new Error("Received PCM totals do not match the packet stream");
  const context = capture.contexts.find(item => item.id === contextId);
  const start = capture.events.find(item => item.type === "recorder.capture-start" && item.contextId === context?.id);
  if (!context || context.sampleRate !== DEFAULT_OUTPUT_RATE || !start || !finite(start.frameOrigin))
    throw new Error("Capture lacks the 48 kHz recorder-clock origin needed for replay");
  return { context, start, contextId, submissionsByReceiveSequence };
}

export function replayCapture(capture, workletSource, { tailSeconds = 0.75 } = {}) {
  if (!finite(tailSeconds) || tailSeconds < 0 || tailSeconds > 10) throw new Error("Invalid replay tail duration");
  const { context, start, contextId, submissionsByReceiveSequence } = validateCapture(capture);
  let Processor;
  let processor;
  const health = [];
  let playedReportSamples = 0;
  let healthAcknowledgements = 0;
  let playedAcknowledgements = 0;
  const globals = {
    AudioWorkletProcessor: class {
      constructor() {
        this.port = {
          onmessage: () => {},
          postMessage: message => {
            if (message?.type === "health") {
              health.push({ ...message, replayAudioTime: globals.currentTime });
              healthAcknowledgements++;
              processor.port.onmessage({ data: { type: "health-ack", epoch: message.epoch } });
            } else if (message?.type === "played" && message.buffer instanceof ArrayBuffer) {
              playedReportSamples += message.buffer.byteLength / 2;
              playedAcknowledgements++;
              processor.port.onmessage({ data: { type: "played-ack", epoch: message.epoch } });
            }
          },
        };
      }
    },
    sampleRate: context.sampleRate,
    currentTime: 0,
    ArrayBuffer,
    Int16Array,
    Float64Array,
    DataView,
    Number,
    Math,
    registerProcessor(name, type) {
      if (name !== "ngn-live-playback") throw new Error("Unexpected playback worklet name");
      Processor = type;
    },
  };
  runInNewContext(workletSource, globals);
  processor = new Processor();

  const timeline = [
    ...capture.events.filter(event => event.type === "playback.control" && event.contextId === contextId && finite(event.audioTime)).map(event => ({
      kind: "control", audioTime: event.audioTime, captureOrder: event.captureOrder,
      type: event.controlType, epoch: event.epoch, realtime: event.realtime,
    })),
    ...capture.received.map(packet => {
      const submitted = submissionsByReceiveSequence.get(packet.transportSequence);
      return { kind: "packet", audioTime: submitted.audioTime, captureOrder: submitted.captureOrder, packet };
    }),
  ].sort((a, b) => a.audioTime - b.audioTime || a.captureOrder - b.captureOrder);

  let epoch = 0;
  let realtime = false;
  let stalePackets = 0;
  let packetIndex = 0;
  let timelineIndex = 0;
  let outputFrames = 0;
  let activeOutputSamples = 0;
  let outputSquareSum = 0;
  let firstActiveAudioTime = null;
  let lastActiveAudioTime = null;
  let peakOutput = 0;
  let maximumBufferedMs = 0;
  let maximumTargetMs = 0;
  let modeControlCount = 0;
  let epochControlCount = 0;
  const startTime = start.frameOrigin / context.sampleRate;
  const finalPacketTime = timeline.filter(item => item.kind === "packet")
    .reduce((latest, item) => Math.max(latest, item.audioTime), -Infinity);
  const firstPacketTime = timeline.filter(item => item.kind === "packet")
    .reduce((earliest, item) => Math.min(earliest, item.audioTime), Infinity);
  if (finalPacketTime - firstPacketTime > 910 || startTime - firstPacketTime > 10 || finalPacketTime - startTime > 920)
    throw new Error("Capture replay duration exceeds the bounded simulation window");
  const endTime = finalPacketTime + tailSeconds;
  let currentTime = startTime;
  const output = new Float32Array(QUANTUM_FRAMES);

  const deliver = item => {
    globals.currentTime = currentTime;
    if (item.kind === "control") {
      if (item.type === "mode") {
        realtime = item.realtime === true;
        modeControlCount++;
        processor.port.onmessage({ data: { type: "mode", epoch: item.epoch, realtime } });
      } else if (item.type === "reset" || item.type === "barrier") {
        epoch = item.epoch;
        epochControlCount++;
        processor.port.onmessage({ data: { type: item.type, epoch } });
      }
      return;
    }
    const packet = item.packet;
    packetIndex++;
    if (packet.epoch !== epoch) stalePackets++;
    processor.port.onmessage({ data: { type: "pcm", epoch: packet.epoch, buffer: decodePacket(packet) } });
  };

  // Apply connection controls captured before the recorder arm point.
  while (timelineIndex < timeline.length && timeline[timelineIndex].audioTime < startTime) deliver(timeline[timelineIndex++]);
  while (currentTime <= endTime) {
    while (timelineIndex < timeline.length && timeline[timelineIndex].audioTime <= currentTime + 1e-9)
      deliver(timeline[timelineIndex++]);
    globals.currentTime = currentTime;
    output.fill(0);
    processor.process([], [[output]]);
    for (const sample of output) {
      const absolute = Math.abs(sample);
      outputSquareSum += sample * sample;
      peakOutput = Math.max(peakOutput, absolute);
      if (absolute > 0.01) {
        activeOutputSamples++;
        if (firstActiveAudioTime === null) firstActiveAudioTime = currentTime;
        lastActiveAudioTime = currentTime;
      }
    }
    outputFrames += QUANTUM_FRAMES;
    currentTime += QUANTUM_FRAMES / context.sampleRate;
  }
  globals.currentTime = currentTime;
  processor.port.onmessage({ data: { type: "health" } });
  const final = health.at(-1) || {};
  const replayHealthSeries = [];
  let lastHealthAt = -Infinity;
  for (const sample of health) {
    if (!finite(sample.replayAudioTime)) continue;
    if ((sample.replayAudioTime - lastHealthAt) * 1000 < 900 && sample !== health.at(-1)) continue;
    const row = {};
    for (const key of ["epoch", "receivedSamples", "playedSamples", "droppedSamples", "underruns", "bufferedMs",
      "targetMs", "latencyRecoveries", "silenceRefills", "peakBufferedMs", "correctionPpm"]) {
      if (finite(sample[key])) row[key] = sample[key];
    }
    row.replayAudioTime = sample.replayAudioTime;
    replayHealthSeries.push(row);
    lastHealthAt = sample.replayAudioTime;
  }
  for (const sample of health) {
    maximumBufferedMs = Math.max(maximumBufferedMs, sample.bufferedMs);
    maximumTargetMs = Math.max(maximumTargetMs, sample.targetMs);
  }
  const outputSamples = outputFrames;
  return {
    schema: "ngn-live-playback-replay/v1",
    replayModel: "modeled-port-submit-time; excludes MessagePort scheduling delay and pre-capture buffered playback state",
    workletSha256: createHash("sha256").update(workletSource).digest("hex"),
    sourceContextId: context.id,
    sourceSampleRate: PCM_RATE,
    outputSampleRate: context.sampleRate,
    quantumFrames: QUANTUM_FRAMES,
    initialEpoch: 0,
    realtimeModeSeen: realtime,
    modeControlCount,
    epochControlCount,
    packetCount: packetIndex,
    staleEpochPackets: stalePackets,
    receivedSamples: final.receivedSamples ?? 0,
    playedSamples: final.playedSamples ?? 0,
    droppedSamples: final.droppedSamples ?? 0,
    underruns: final.underruns ?? 0,
    latencyRecoveries: final.latencyRecoveries ?? 0,
    silenceRefills: final.silenceRefills ?? 0,
    maximumBufferedMs,
    maximumTargetMs,
    replayHealthSeries,
    healthAcknowledgements,
    playedAcknowledgements,
    playedReportSamples,
    output: {
      renderedSamples: outputSamples,
      activeSamples: activeOutputSamples,
      activeRms: Math.sqrt(outputSquareSum / Math.max(1, outputSamples)),
      peak: peakOutput,
      firstActiveAudioTime,
      lastActiveAudioTime,
    },
  };
}

function main() {
  const capturePath = process.argv[2];
  if (!capturePath) throw new Error("Usage: node replay-received.mjs private-capture.json");
  const capture = JSON.parse(readFileSync(capturePath, "utf8"));
  const workletUrl = new URL("../../../src/nagents/web-ui/public/assets/live-playback.js", import.meta.url);
  const workletSource = readFileSync(workletUrl, "utf8");
  process.stdout.write(`${JSON.stringify(replayCapture(capture, workletSource))}\n`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
