import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

type WorkletMessage = { type: "samples"; samples: Float32Array } | { type: "stopped"; limited: boolean };
type Processor = {
  process: (inputs: Float32Array[][], outputs?: Float32Array[][]) => boolean;
  port: { onmessage: (event: { data: { type: string } }) => void };
};

function worklet(maxFrames: number) {
  const messages: WorkletMessage[] = [];
  let constructor!: new (options: { processorOptions: { maxFrames: number } }) => Processor;
  runInNewContext(readFileSync(new URL("./pcm-worklet.js", import.meta.url), "utf8"), {
    Float32Array,
    AudioWorkletProcessor: class {
      port = { onmessage: () => undefined, postMessage: (message: WorkletMessage) => messages.push(message) };
    },
    registerProcessor: (name: string, value: typeof constructor) => {
      assert.equal(name, "ngn-pcm-capture");
      constructor = value;
    },
  });
  return { processor: new constructor({ processorOptions: { maxFrames } }), messages };
}

test("the actual worklet averages channels and flushes a final partial buffer before its stop acknowledgement", () => {
  const { processor, messages } = worklet(48000);
  const output = new Float32Array(4);
  processor.process([[new Float32Array([1, 0, -1, 0]), new Float32Array([0, 1, 0, -1])]], [[output]]);
  assert.equal(messages.length, 0);
  processor.port.onmessage({ data: { type: "stop" } });
  assert.equal(messages.length, 2);
  assert.equal(messages[0].type, "samples");
  if (messages[0].type === "samples") assert.deepEqual([...messages[0].samples], [0.5, 0.5, -0.5, -0.5]);
  assert.deepEqual(JSON.parse(JSON.stringify(messages[1])), { type: "stopped", limited: false });
  assert.deepEqual([...output], [0, 0, 0, 0]);
  processor.port.onmessage({ data: { type: "stop" } });
  assert.equal(processor.process([[new Float32Array(128)]]), false);
  assert.equal(messages.length, 2);
});

test("the worklet independently caps device-rate frames even when main-thread timers cannot run", () => {
  const { processor, messages } = worklet(3000);
  for (let index = 0; index < 30; index++) processor.process([[new Float32Array(128).fill(0.25)]]);
  const lengths = messages.flatMap((message) => message.type === "samples" ? [message.samples.length] : []);
  assert.deepEqual(lengths, [2048, 952]);
  assert.deepEqual(JSON.parse(JSON.stringify(messages.at(-1))), { type: "stopped", limited: true });
});
