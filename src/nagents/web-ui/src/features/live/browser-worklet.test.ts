import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

test("capture worklet converts device-rate mono speech into exact 20 ms PCM16 frames", () => {
  const frames: ArrayBuffer[] = [];
  let processor!: new () => { process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean };
  class AudioWorkletProcessorFixture {
    port = { postMessage: (buffer: ArrayBuffer) => { frames.push(buffer); } };
  }
  runInNewContext(readFileSync(new URL("../../../public/assets/live-capture.js", import.meta.url), "utf8"), {
    AudioWorkletProcessor: AudioWorkletProcessorFixture,
    sampleRate: 48_000,
    Int16Array,
    Math,
    registerProcessor: (name: string, type: typeof processor) => { assert.equal(name, "ngn-live-capture"); processor = type; },
  });
  const capture = new processor();
  for (let i = 0; i < 8; i++) {
    const output = new Float32Array(120).fill(1);
    assert.equal(capture.process([[new Float32Array(120).fill(0.5)]], [[output]]), true);
    assert.ok(output.every((sample) => sample === 0), "microphone must never play locally");
  }
  assert.equal(frames.length, 1);
  assert.equal(frames[0].byteLength, 960);
  assert.ok(Array.from(new Int16Array(frames[0])).every((sample) => sample === 16384));
});
