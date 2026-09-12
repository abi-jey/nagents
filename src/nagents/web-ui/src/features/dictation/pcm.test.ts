import assert from "node:assert/strict";
import test from "node:test";
import { WavEncoder } from "./pcm.js";

test("WAV is metadata-free little-endian mono PCM16 at 16 kHz, with clipped signed samples", async () => {
  const encoder = new WavEncoder(16000, 1, 36096);
  encoder.push(new Float32Array([-2, -1, -0.5, 0, 0.5, 1, 2]));
  const blob = encoder.finish();
  assert.equal(blob.type, "audio/wav");
  assert.equal(blob.size, 44 + 14);
  const bytes = await blob.arrayBuffer();
  const view = new DataView(bytes);
  assert.equal(new TextDecoder().decode(bytes.slice(0, 4)), "RIFF");
  assert.equal(view.getUint32(4, true), blob.size - 8);
  assert.equal(new TextDecoder().decode(bytes.slice(8, 16)), "WAVEfmt ");
  assert.equal(view.getUint32(16, true), 16);
  assert.equal(view.getUint16(20, true), 1);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getUint32(28, true), 32000);
  assert.equal(view.getUint16(32, true), 2);
  assert.equal(view.getUint16(34, true), 16);
  assert.equal(new TextDecoder().decode(bytes.slice(36, 40)), "data");
  assert.equal(view.getUint32(40, true), 14);
  assert.deepEqual(Array.from({ length: 7 }, (_, index) => view.getInt16(44 + index * 2, true)),
    [-32768, -32768, -16384, 0, 16384, 32767, 32767]);
});

for (const rate of [8000, 16000, 44100, 48000, 96000, 192000]) {
  test(`${rate} Hz capture resamples to exactly 16000 samples independent of chunk boundaries`, async () => {
    const samples = Float32Array.from({ length: rate }, (_, index) => Math.sin(2 * Math.PI * 440 * index / rate) * 0.5);
    const whole = new WavEncoder(rate, 1, 36096);
    whole.push(samples);
    const chunked = new WavEncoder(rate, 1, 36096);
    for (let index = 0; index < samples.length; index += 127) chunked.push(samples.subarray(index, index + 127));
    const blob = chunked.finish();
    assert.equal(blob.size, 32044);
    assert.deepEqual(await blob.arrayBuffer(), await whole.finish().arrayBuffer());
    const data = new DataView(await blob.arrayBuffer());
    // A synthetic speech-band tone keeps its frequency and amplitude after resampling.
    let error = 0;
    for (let index = 0; index < 16000; index++) {
      const expected = Math.sin(2 * Math.PI * 440 * index / 16000) * 0.5;
      error += (data.getInt16(44 + index * 2, true) / 32768 - expected) ** 2;
    }
    assert.ok(error / 16000 < 0.002);
  });
}

test("partial resampling windows do not invent audio duration", () => {
  const encoder = new WavEncoder(44100, 1, 36096);
  encoder.push(new Float32Array(442));
  assert.equal(encoder.finish().size, 44 + Math.floor(442 * 16000 / 44100) * 2);
});

test("capture buffers reject excess duration, malformed limits, and nonfinite audio", () => {
  for (const rate of [0, 7999, 48000.5, Infinity, 384001])
    assert.throws(() => new WavEncoder(rate, 1, 36096), /Unsupported/);
  for (const seconds of [0, -1, 301, 1.5])
    assert.throws(() => new WavEncoder(16000, seconds, 9604096), /Unsupported/);
  assert.throws(() => new WavEncoder(16000, 1, 32043), /Unsupported/);
  const encoder = new WavEncoder(48000, 1, 36096);
  encoder.push(new Float32Array(48000));
  assert.throws(() => encoder.push(new Float32Array(1)), /duration limit/);
  assert.equal(encoder.finish().size, 32044);
  assert.throws(() => new WavEncoder(16000, 1, 36096).push(new Float32Array([NaN])), /Invalid microphone/);
  assert.throws(() => new WavEncoder(16000, 1, 36096).finish(), /No audio/);
});
