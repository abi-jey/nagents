import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

function recorder() {
  let Processor;
  const messages = [];
  const globals = {
    AudioWorkletProcessor: class {
      port = { postMessage: message => messages.push(message), onmessage: undefined };
    },
    sampleRate: 48000,
    currentFrame: 0,
    registerProcessor: (_name, processor) => { Processor = processor; },
  };
  runInNewContext(readFileSync(new URL("./capture-worklet.js", import.meta.url), "utf8"), globals);
  const instance = new Processor();
  const quantum = (frame, value = .25, length = 128) => {
    globals.currentFrame = frame;
    const samples = value instanceof Float32Array ? value : new Float32Array(length).fill(value);
    instance.process([[samples]], [[new Float32Array(samples.length)]]);
  };
  return { messages, quantum, arm: () => instance.port.onmessage({ data: "arm" }),
    stop: () => instance.port.onmessage({ data: "stop" }) };
}

test("v2 coordinates are sample-monotonic across variable quanta and preserve the partial tail", () => {
  const rig = recorder();
  rig.arm();
  rig.quantum(2000, .1, 1024);
  rig.quantum(3024, .2, 768);
  rig.quantum(3792, .3, 512);
  rig.stop();

  const start = rig.messages[0];
  assert.deepEqual({ type: start.type, schema: start.schema, frame: start.frame, frameOrigin: start.frameOrigin,
    observedFrame: start.observedFrame, sampleOffset: start.sampleOffset, sequence: start.sequence },
  { type: "capture-start", schema: 2, frame: 2000, frameOrigin: 2000,
    observedFrame: 2000, sampleOffset: 0, sequence: 0 });
  const blocks = rig.messages.filter(item => item.samples);
  assert.deepEqual(blocks.map(item => ({ frame: item.frame, sampleOffset: item.sampleOffset,
    sequence: item.sequence, observedFrame: item.observedFrame, length: item.samples.length })), [
    { frame: 2000, sampleOffset: 0, sequence: 0, observedFrame: 2000, length: 2048 },
    { frame: 4048, sampleOffset: 2048, sequence: 1, observedFrame: 4048, length: 256 },
  ]);
  assert.deepEqual(Array.from(blocks[0].samples), [
    ...new Array(1024).fill(Math.fround(.1)),
    ...new Array(768).fill(Math.fround(.2)),
    ...new Array(256).fill(Math.fround(.3)),
  ]);
  assert.deepEqual(Array.from(blocks[1].samples), new Array(256).fill(Math.fround(.3)));
  const stop = rig.messages.at(-1);
  assert.deepEqual({ type: stop.type, schema: stop.schema, finalFrame: stop.finalFrame,
    observedFinalFrame: stop.observedFinalFrame, frameOrigin: stop.frameOrigin,
    samplesCaptured: stop.samplesCaptured, blocksCaptured: stop.blocksCaptured },
  { type: "capture-stop", schema: 2, finalFrame: 4304, observedFinalFrame: 4304,
    frameOrigin: 2000, samplesCaptured: 2304, blocksCaptured: 2 });
});

test("paired raw clock jumps preserve each sample and monotonic frame/offset/sequence", () => {
  const rig = recorder();
  rig.arm();
  rig.quantum(1000, .1);
  rig.quantum(1000, .2); // -128 raw-frame label jump; captured samples continue.
  rig.quantum(1256, .3); // +128 raw-frame label jump; captured samples continue.
  rig.quantum(1384, .4);
  rig.stop();

  const discontinuities = rig.messages.filter(item => item.type === "clock-discontinuity");
  assert.deepEqual(discontinuities.map(item => ({ from: item.fromFrame, to: item.toFrame,
    capturedSamples: item.capturedSamples, sampleOffset: item.sampleOffset, sequence: item.sequence })), [
    { from: 1128, to: 1000, capturedSamples: 128, sampleOffset: 128, sequence: 1 },
    { from: 1128, to: 1256, capturedSamples: 256, sampleOffset: 256, sequence: 2 },
  ]);
  const blocks = rig.messages.filter(item => item.samples);
  assert.deepEqual(blocks.map(item => ({ frame: item.frame, sampleOffset: item.sampleOffset,
    sequence: item.sequence, observedFrame: item.observedFrame, length: item.samples.length })), [
    { frame: 1000, sampleOffset: 0, sequence: 0, observedFrame: 1000, length: 128 },
    { frame: 1128, sampleOffset: 128, sequence: 1, observedFrame: 1000, length: 128 },
    { frame: 1256, sampleOffset: 256, sequence: 2, observedFrame: 1256, length: 256 },
  ]);
  const captured = blocks.flatMap(item => Array.from(item.samples));
  assert.deepEqual(captured, [
    ...new Array(128).fill(Math.fround(.1)), ...new Array(128).fill(Math.fround(.2)),
    ...new Array(128).fill(Math.fround(.3)), ...new Array(128).fill(Math.fround(.4)),
  ]);
  const stop = rig.messages.at(-1);
  assert.deepEqual({ finalFrame: stop.finalFrame, observedFinalFrame: stop.observedFinalFrame,
    samplesCaptured: stop.samplesCaptured, blocksCaptured: stop.blocksCaptured },
  { finalFrame: 1512, observedFinalFrame: 1512, samplesCaptured: 512, blocksCaptured: 3 });
});

test("warmup jumps emit no PCM; arm anchors v2 frame origin on the first captured quantum", () => {
  const rig = recorder();
  rig.quantum(0);
  rig.quantum(640);
  assert.deepEqual({ ...rig.messages[0] }, {
    type: "clock-discontinuity", schema: 2, fromFrame: 128, toFrame: 640, observedFrame: 640,
    capturedSamples: 0, sampleOffset: 0, sequence: 0, sampleRate: 48000, phase: "warmup",
  });
  rig.arm();
  rig.quantum(768, .5);
  rig.stop();
  assert.deepEqual({ ...rig.messages[1] }, {
    type: "capture-start", schema: 2, frame: 768, frameOrigin: 768, observedFrame: 768,
    sampleOffset: 0, sequence: 0, sampleRate: 48000,
  });
  const block = rig.messages.find(item => item.samples);
  assert.equal(block.frame, 768);
  assert.equal(block.sampleOffset, 0);
  assert.equal(block.sequence, 0);
  assert.equal(block.observedFrame, 768);
  assert.equal(block.samples.length, 128);
  assert.equal(block.samples[0], .5);
  const stop = rig.messages.at(-1);
  assert.equal(stop.finalFrame, 896);
  assert.equal(stop.observedFinalFrame, 896);
  assert.equal(stop.samplesCaptured, 128);
  assert.equal(stop.blocksCaptured, 1);
});
