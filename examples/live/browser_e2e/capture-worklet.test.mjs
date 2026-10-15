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
  const quantum = (frame, value = .25) => {
    globals.currentFrame = frame;
    instance.process([[new Float32Array(128).fill(value)]], [[new Float32Array(128)]]);
  };
  return { messages, quantum, arm: () => instance.port.onmessage({ data: "arm" }),
    stop: () => instance.port.onmessage({ data: "stop" }) };
}

test("continuous quanta retain exact frame positions and flush the partial tail", () => {
  const rig = recorder();
  rig.arm();
  for (let i = 0; i < 17; i++) rig.quantum(i * 128);
  rig.stop();
  assert.equal(rig.messages[0].type, "capture-start");
  assert.equal(rig.messages[0].frame, 0);
  assert.deepEqual(rig.messages.filter(item => item.samples).map(item => [item.frame, item.samples.length]), [[0, 2048], [2048, 128]]);
  assert.equal(rig.messages.at(-1).type, "capture-stop");
  assert.equal(rig.messages.at(-1).finalFrame, 2176);
  assert.equal(rig.messages.at(-1).armed, true);
});

test("a clock jump inside a partial block is explicit and never concatenated away", () => {
  const rig = recorder();
  rig.arm();
  rig.quantum(0);
  rig.quantum(640, .5);
  rig.stop();
  assert.equal(rig.messages[1].frame, 0);
  assert.equal(rig.messages[1].samples.length, 128);
  assert.equal(rig.messages[2].type, "clock-discontinuity");
  assert.equal(rig.messages[2].phase, "recording");
  assert.equal(rig.messages[2].fromFrame, 128);
  assert.equal(rig.messages[2].toFrame, 640);
  assert.equal(rig.messages[3].frame, 640);
  assert.equal(rig.messages[3].samples.length, 128);
  assert.equal(rig.messages[3].samples[0], .5);
  assert.equal(rig.messages[4].type, "capture-stop");
  assert.equal(rig.messages[4].finalFrame, 768);
});

test("warmup jumps are logged without PCM; arm acknowledges the actual next quantum", () => {
  const rig = recorder();
  rig.quantum(0);
  rig.quantum(640);
  assert.equal(rig.messages.length, 1);
  assert.equal(rig.messages[0].type, "clock-discontinuity");
  assert.equal(rig.messages[0].phase, "warmup");
  rig.arm();
  rig.quantum(768);
  rig.stop();
  assert.equal(rig.messages[1].type, "capture-start");
  assert.equal(rig.messages[1].frame, 768);
  assert.equal(rig.messages[2].frame, 768);
  assert.equal(rig.messages[2].samples.length, 128);
  assert.equal(rig.messages[3].type, "capture-stop");
  assert.equal(rig.messages[3].finalFrame, 896);
});
