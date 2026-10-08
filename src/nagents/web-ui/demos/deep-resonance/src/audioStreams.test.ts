import assert from "node:assert/strict";
import test from "node:test";
import { createPcmAudio, type PcmFormat, type PcmEncoding } from "./audioStreams";

const mono: PcmFormat = { encoding: "s16le", sampleRate: 8000, channels: 1 };

function pcm(frames: number, encoding: PcmEncoding, channels: 1 | 2, value: (frame: number, channel: number) => number): Uint8Array {
  const width = encoding === "s16le" ? 2 : 4, bytes = new Uint8Array(frames * width * channels), view = new DataView(bytes.buffer);
  for (let frame = 0; frame < frames; frame++) for (let channel = 0; channel < channels; channel++) {
    const offset = (frame * channels + channel) * width, sample = value(frame, channel);
    if (encoding === "s16le") view.setInt16(offset, Math.max(-32768, Math.min(32767, Math.round(sample * 32768))), true);
    else view.setFloat32(offset, sample, true);
  }
  return bytes;
}

function fixture(format: PcmFormat = mono, maxBufferedSeconds = 2) {
  let now = 0;
  const audio = createPcmAudio({ inputFormat: format, outputFormat: format, maxBufferedSeconds, clock: () => now });
  return { audio, time: (seconds: number) => { now = seconds; } };
}

function near(actual: number, expected: number, tolerance = .001) {
  assert(Math.abs(actual - expected) <= tolerance, `${actual} differs from ${expected}`);
}

test("scheduled PCM starts on its clock, channels stay independent, and close drains without stale levels", () => {
  const f = fixture();
  try {
    f.audio.output.write(pcm(256, "s16le", 1, () => .5), { at: 1 });
    f.audio.output.close();
    f.time(.999); assert.equal(f.audio.sample().output.active, false);
    f.time(1.031875); const sample = f.audio.sample();
    assert.equal(sample.input.active, false); assert.equal(sample.input.rms, 0);
    assert.equal(sample.output.active, true); near(sample.output.rms, .5);
    f.time(1.032); assert.deepEqual(f.audio.sample().output, { active: false, rms: 0, low: 0, mid: 0, high: 0 });
    assert.equal(f.audio.output.stats().bufferedFrames, 0);
    assert.throws(() => f.audio.output.write(new Uint8Array([0, 0])), { name: "InvalidStateError" });
  } finally { f.audio.dispose(); }
});

test("s16 little-endian decoding respects byte offsets and signed full scale", () => {
  const f = fixture();
  try {
    const storage = new Uint8Array(516), bytes = storage.subarray(2, 514);
    for (let i = 0; i < 256; i++) { bytes[2 * i] = 0; bytes[2 * i + 1] = 128; }
    f.audio.input.write(bytes, { at: 0 }); f.time(.031875);
    near(f.audio.sample().input.rms, 1, 1e-8);
    bytes.fill(0); near(f.audio.sample().input.rms, 1, 1e-8);
  } finally { f.audio.dispose(); }
});

test("fragmented stereo float frames preserve alignment and opposite phases do not cancel", () => {
  const f = fixture({ encoding: "f32le", sampleRate: 8000, channels: 2 });
  try {
    const bytes = pcm(256, "f32le", 2, (_, channel) => channel ? -.5 : .5), steps = [1, 2, 7, 3, 19];
    for (let offset = 0, part = 0; offset < bytes.length; part++) {
      const end = Math.min(bytes.length, offset + steps[part % steps.length]);
      f.audio.input.write(bytes.subarray(offset, end), offset ? undefined : { at: 0 }); offset = end;
    }
    f.time(.031875); near(f.audio.sample().input.rms, .5, 1e-8);
    assert.equal(f.audio.input.stats().pendingBytes, 0); assert.equal(f.audio.input.stats().acceptedFrames, 256);
  } finally { f.audio.dispose(); }
});

test("overlapping timestamped windows trim duplicates even after sample drain", () => {
  const f = fixture();
  try {
    f.audio.output.write(pcm(256, "s16le", 1, () => .25), { at: 0 });
    f.audio.output.write(pcm(256, "s16le", 1, () => .5), { at: .016 });
    f.time(.047875); near(f.audio.sample().output.rms, Math.sqrt((.25 ** 2 + .5 ** 2) / 2));
    assert.equal(f.audio.output.stats().acceptedFrames, 384); assert.equal(f.audio.output.stats().overlapFrames, 128);
    f.time(1); assert.equal(f.audio.sample().output.active, false);
    f.audio.output.write(pcm(256, "s16le", 1, () => 1), { at: .016 });
    assert.equal(f.audio.output.stats().acceptedFrames, 384); assert.equal(f.audio.sample().output.active, false);
    f.audio.output.reset(); f.time(.031875);
    f.audio.output.write(pcm(256, "s16le", 1, () => .75), { at: 0 });
    near(f.audio.sample().output.rms, .75);
  } finally { f.audio.dispose(); }
});

test("overflow drops oldest frames deterministically and reset clears fragments immediately", () => {
  const f = fixture(mono, .032);
  try {
    f.audio.input.write(pcm(512, "s16le", 1, frame => frame < 256 ? .2 : .7), { at: 1 });
    assert.equal(f.audio.input.stats().bufferedFrames, 256); assert.equal(f.audio.input.stats().droppedFrames, 256);
    f.time(1.063875); near(f.audio.sample().input.rms, .7);
    f.audio.input.write(new Uint8Array([127])); assert.equal(f.audio.input.stats().pendingBytes, 1);
    f.audio.input.reset(); assert.equal(f.audio.input.stats().bufferedFrames, 0); assert.equal(f.audio.input.stats().pendingBytes, 0);
    assert.equal(f.audio.sample().input.active, false);
  } finally { f.audio.dispose(); }
});

test("float sanitization prevents nonfinite levels and empty capture expires within its bounded window", () => {
  const f = fixture({ encoding: "f32le", sampleRate: 8000, channels: 1 });
  try {
    f.audio.input.write(pcm(256, "f32le", 1, () => NaN), { at: 0 }); f.time(.031875);
    assert.deepEqual(f.audio.sample().input, { active: true, rms: 0, low: 0, mid: 0, high: 0 });
    f.audio.input.reset(); f.audio.input.write(pcm(256, "f32le", 1, i => i % 2 ? -2 : 2), { at: 0 });
    const signal = f.audio.sample().input; near(signal.rms, 1); assert(Object.values(signal).every(value => typeof value === "boolean" || Number.isFinite(value)));
    f.time(.066); assert.equal(f.audio.sample().input.active, false);
  } finally { f.audio.dispose(); }
});

test("internal spectral analysis separates low, mid and high energy without host DSP", () => {
  for (const [frequency, expected] of [[120, "low"], [1100, "mid"], [4800, "high"]] as const) {
    const f = fixture({ encoding: "f32le", sampleRate: 48000, channels: 2 });
    try {
      f.audio.output.write(pcm(1536, "f32le", 2, (i, channel) => .2 * Math.sin(2 * Math.PI * frequency * i / 48000) * (channel ? -1 : 1)), { at: 0 });
      f.time(1535 / 48000); const frame = f.audio.sample().output;
      near(frame.rms, .2 / Math.SQRT2, .003); assert(frame[expected] > .12);
      for (const band of ["low", "mid", "high"] as const) if (band !== expected) assert(frame[band] < frame[expected] * .1);
    } finally { f.audio.dispose(); }
  }
});

test("WritableStream pipeTo and consume handle fragmented bytes and release source locks", async () => {
  for (const method of ["pipe", "consume"] as const) {
    const f = fixture(), bytes = pcm(256, "s16le", 1, () => .4);
    try {
      const source = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(bytes.subarray(0, 3)); controller.enqueue(bytes.subarray(3)); controller.close(); } });
      if (method === "pipe") await source.pipeTo(f.audio.input.writable); else await f.audio.input.consume(source);
      assert.equal(source.locked, false); assert.equal(f.audio.input.stats().closed, true);
      f.time(.031875); near(f.audio.sample().input.rms, .4);
      f.time(.032); assert.equal(f.audio.sample().input.active, false);
    } finally { f.audio.dispose(); }
  }
});

test("aborting a pending consumer cancels the reader, clears queued audio and closes writes", async () => {
  const f = fixture(), abort = new AbortController(), reason = new Error("test abort"); let cancelled: unknown;
  const source = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(pcm(256, "s16le", 1, () => .5)); }, cancel(value: unknown) { cancelled = value; } });
  try {
    const consuming = f.audio.input.consume(source, { signal: abort.signal }); await Promise.resolve(); await Promise.resolve();
    abort.abort(reason); await assert.rejects(consuming, error => error === reason);
    assert.equal(cancelled, reason); assert.equal(source.locked, false); assert.equal(f.audio.input.stats().bufferedFrames, 0);
    assert.equal(f.audio.input.stats().closed, true); assert.equal(f.audio.sample().input.active, false);
  } finally { f.audio.dispose(); }
});

test("dispose cancels outstanding consumption and becomes inert without leaking audio", async () => {
  const f = fixture(); let cancelled = false;
  const source = new ReadableStream<Uint8Array>({ cancel() { cancelled = true; } });
  const consuming = f.audio.output.consume(source);
  f.audio.dispose(); f.audio.dispose();
  await assert.rejects(consuming, { name: "AbortError" });
  assert.equal(cancelled, true); assert.equal(source.locked, false);
  assert.equal(f.audio.sample().input.active, false); assert.equal(f.audio.sample().output.active, false);
  assert.throws(() => f.audio.output.write(new Uint8Array([0, 0])), { name: "InvalidStateError" });
});

test("manual close cancels a consumer but retains already scheduled frames until their end", async () => {
  const f = fixture();
  try {
    const source = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(pcm(256, "s16le", 1, () => .6)); } });
    const consuming = f.audio.input.consume(source); await Promise.resolve(); await Promise.resolve();
    f.audio.input.close(); await assert.rejects(consuming, { name: "InvalidStateError" });
    f.time(.031875); near(f.audio.sample().input.rms, .6); assert.equal(source.locked, false);
  } finally { f.audio.dispose(); }
});

test("validation rejects invalid formats, timestamps, fragmented timing and writes after stream closure", async () => {
  for (const format of [{ ...mono, sampleRate: 0 }, { ...mono, sampleRate: 44100.5 }, { ...mono, channels: 3 }, { ...mono, encoding: "mp3" }]) {
    assert.throws(() => createPcmAudio({ inputFormat: format as PcmFormat, outputFormat: mono }));
  }
  assert.throws(() => createPcmAudio({ inputFormat: mono, outputFormat: mono, maxBufferedSeconds: Infinity }));
  const f = fixture();
  try {
    assert.throws(() => f.audio.input.write(new Uint8Array([0, 0]), { at: NaN }), RangeError);
    f.audio.input.write(new Uint8Array([0]), { at: 1 });
    assert.throws(() => f.audio.input.write(new Uint8Array([0]), { at: 2 }), RangeError);
    assert.equal(f.audio.input.stats().pendingBytes, 1); f.audio.input.close(); assert.equal(f.audio.input.stats().pendingBytes, 0);
    const writer = f.audio.input.writable.getWriter(); await assert.rejects(writer.write(new Uint8Array([0, 0])), { name: "InvalidStateError" }); writer.releaseLock();
  } finally { f.audio.dispose(); }
});

test("upstream and malformed-chunk errors discard buffered data and cancel the source", async () => {
  const f = fixture(); let cancelled = false;
  try {
    const source = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue("bad" as unknown as Uint8Array); }, cancel() { cancelled = true; } });
    await assert.rejects(f.audio.input.consume(source), TypeError); assert.equal(cancelled, true); assert.equal(source.locked, false);
    const failure = new Error("source failed"), failed = new ReadableStream<Uint8Array>({ start(controller) { controller.error(failure); } });
    await assert.rejects(f.audio.output.consume(failed), error => error === failure); assert.equal(f.audio.sample().output.active, false);
  } finally { f.audio.dispose(); }
});
