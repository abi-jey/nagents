import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

for (const rate of [8_000, 16_000, 24_000, 44_100, 48_000, 96_000]) test(`capture worklet converts ${rate} Hz speech into exact 20 ms PCM16 frames`, () => {
  const frames: ArrayBuffer[] = [];
  let processor!: new () => { process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean };
  class AudioWorkletProcessorFixture {
    port = { postMessage: (buffer: ArrayBuffer) => { frames.push(buffer); } };
  }
  runInNewContext(readFileSync(new URL("../../../public/assets/live-capture.js", import.meta.url), "utf8"), {
    AudioWorkletProcessor: AudioWorkletProcessorFixture,
    sampleRate: rate,
    Int16Array,
    Math,
    registerProcessor: (name: string, type: typeof processor) => { assert.equal(name, "ngn-live-capture"); processor = type; },
  });
  const capture = new processor();
  // One second split across the same 128-sample render quanta as the browser,
  // including a partial last quantum, must retain timing across callbacks.
  for (let i = 0; i < rate; i += 128) {
    const size = Math.min(128, rate - i), output = new Float32Array(size).fill(1);
    assert.equal(capture.process([[new Float32Array(size).fill(0.5)]], [[output]]), true);
    assert.ok(output.every((sample) => sample === 0), "microphone must never play locally");
  }
  assert.equal(frames.length, 50);
  for (const frame of frames) {
    assert.equal(frame.byteLength, 960);
    assert.ok(Array.from(new Int16Array(frame)).every((sample) => sample === 16384));
  }
});
