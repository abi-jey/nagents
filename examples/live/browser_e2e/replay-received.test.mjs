import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { replayCapture } from "./replay-received.mjs";

const worklet = readFileSync(new URL("../../../src/nagents/web-ui/public/assets/live-playback.js", import.meta.url), "utf8");
function packet(sequence, audioTime, epoch = 0, sampleOffset = sequence * 480) {
  const samples = new Int16Array(480);
  for (let index = 0; index < samples.length; index++) samples[index] = 8000 + (index % 11);
  const bytes = Buffer.from(samples.buffer);
  return { schema: 1, sequence, captureOrder: sequence + 100, sampleOffset,
    transportSequence: sequence, transportReceivedSamples: sampleOffset + samples.length,
    receivedSamples: sampleOffset + samples.length, samples: samples.length, bytes: bytes.length,
    sampleRate: 24000, contextId: 0, epoch, realtime: true, audioTime, monotonicMs: audioTime * 1000,
    wallMs: audioTime * 1000, pcm16: bytes.toString("base64") };
}
function capture(packets, controls = [{ controlType: "mode", epoch: 0, realtime: true, audioTime: 0, captureOrder: 0 }]) {
  return { schema: "ngn-browser-voice-e2e/v2", omitted: 0,
    contexts: [{ id: 0, sampleRate: 48000, frameOrigin: 0 }],
    events: [
      { type: "recorder.capture-start", contextId: 0, frameOrigin: 0, sampleRate: 48000 },
      ...controls.map(item => ({ type: "playback.control", contextId: 0, ...item })),
      ...packets.map(item => ({ type: "playback.submit", contextId: 0, receivedSequence: item.transportSequence,
        receivedSamples: item.receivedSamples, transportReceivedSamples: item.transportReceivedSamples,
        captureOrder: item.captureOrder + 100, audioTime: item.audioTime, epoch: item.epoch,
        samples: item.samples, bytes: item.bytes, submittedSamples: item.receivedSamples })),
    ],
    health: [],
    received: packets,
    receivedPcm: { sampleRate: 24000, sampleFormat: "s16le", packetCount: packets.length,
      capturedPackets: packets.length, samples: packets.length * 480, capturedSamples: packets.length * 480,
      capturedBytes: packets.length * 960, omittedBytes: 0, omittedPackets: 0 },
  };
}

test("replay feeds captured packet PCM to the production worklet in order with real-time mode and acknowledgements", () => {
  const packets = Array.from({ length: 90 }, (_, index) => packet(index, index * 0.02));
  const result = replayCapture(capture(packets), worklet, { tailSeconds: 0.8 });
  assert.equal(result.packetCount, packets.length);
  assert.equal(result.receivedSamples, packets.length * 480);
  assert.equal(result.playedSamples, result.receivedSamples);
  assert.equal(result.droppedSamples, 0);
  assert.equal(result.staleEpochPackets, 0);
  assert.equal(result.realtimeModeSeen, true);
  assert(result.healthAcknowledgements > 0);
  assert(result.playedAcknowledgements > 0);
  assert(result.replayHealthSeries.length > 0);
  assert(result.replayHealthSeries.length <= result.healthAcknowledgements);
  assert(result.replayHealthSeries.every(row => Number.isFinite(row.replayAudioTime)));
  assert(result.output.activeSamples > 40_000);
  assert(result.output.peak > 0.1 && result.output.peak < 1);
});

test("replay applies captured epoch transitions before later packets and reports a source gap", () => {
  const packets = [
    ...Array.from({ length: 20 }, (_, index) => packet(index, index * 0.02, 0, index * 480)),
    ...Array.from({ length: 30 }, (_, index) => packet(index + 20, 0.70 + index * 0.02, 1, (index + 20) * 480)),
  ];
  const controls = [
    { controlType: "mode", epoch: 0, realtime: true, audioTime: 0, captureOrder: 0 },
    { controlType: "reset", epoch: 1, realtime: true, audioTime: 0.65, captureOrder: 21 },
  ];
  const result = replayCapture(capture(packets, controls), worklet, { tailSeconds: 0.8 });
  assert.equal(result.packetCount, packets.length);
  assert.equal(result.receivedSamples, packets.length * 480);
  assert.equal(result.droppedSamples, 0);
  assert.equal(result.staleEpochPackets, 0);
  assert(result.underruns >= 1);
  assert.equal(result.epochControlCount, 1);
  assert(result.output.activeSamples > 10_000);
});

test("replay rejects captures that hit PCM omission bounds instead of silently replaying partial input", () => {
  const source = capture([packet(0, 0)]);
  source.receivedPcm.omittedBytes = 2;
  source.receivedPcm.omittedPackets = 1;
  assert.throws(() => replayCapture(source, worklet), /omitted PCM/);
});

test("legacy v2 captures without a received ledger are explicitly not replayable", () => {
  const source = capture([packet(0, 0)]);
  delete source.received;
  delete source.receivedPcm;
  assert.throws(() => replayCapture(source, worklet), /with received network PCM/);
});

test("replay rejects a packet sequence gap and inconsistent totals", () => {
  const missing = capture([packet(0, 0), packet(2, 0.04, 0, 960)]);
  missing.receivedPcm.packetCount = 2;
  assert.throws(() => replayCapture(missing, worklet), /missing or reordered packet/);
  const totals = capture([packet(0, 0)]);
  totals.receivedPcm.capturedBytes++;
  assert.throws(() => replayCapture(totals, worklet), /totals do not match/);
});

test("replay rejects an over-limit declared or actual source stream", () => {
  const declared = capture([packet(0, 0)]);
  declared.receivedPcm.capturedBytes = 30_000_002;
  assert.throws(() => replayCapture(declared, worklet), /offline replay bound/);
  const invalid = capture([packet(0, 0)]);
  invalid.received[0].bytes = 48_002;
  assert.throws(() => replayCapture(invalid, worklet), /length is inconsistent|worklet bound/);
});

test("replay rejects mixed playback contexts rather than combining clocks", () => {
  const source = capture([packet(0, 0), packet(1, 0.02, 0, 480)]);
  source.received[1].contextId = 7;
  assert.throws(() => replayCapture(source, worklet), /single playback context/);
});

test("replay rejects unknown contexts and empty PCM packets", () => {
  const unknown = capture([packet(0, 0)]);
  unknown.received[0].contextId = null;
  assert.throws(() => replayCapture(unknown, worklet), /single playback context|invalid playback context/);
  const empty = capture([packet(0, 0)]);
  empty.received[0].pcm16 = "";
  empty.received[0].bytes = 0;
  empty.received[0].samples = 0;
  assert.throws(() => replayCapture(empty, worklet), /length is inconsistent|sample clock\/order metadata/);
});

test("replay requires each captured receive packet to have reached the playback port", () => {
  const source = capture([packet(0, 0)]);
  source.events = source.events.filter(event => event.type !== "playback.submit");
  assert.throws(() => replayCapture(source, worklet), /lacks a matching playback submission/);
});
