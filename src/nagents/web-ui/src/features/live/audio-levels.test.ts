import assert from "node:assert/strict";
import test from "node:test";
import { audioLevels } from "./audio-levels.js";

class Node {
  connections = new Set<object>();
  connect(target: object) { this.connections.add(target); return target; }
  disconnect(target?: object) { if (target) this.connections.delete(target); else this.connections.clear(); }
}
class Analyser extends Node {
  fftSize = 0;
  amplitude = 0;
  reads = 0;
  getFloatTimeDomainData(samples: Float32Array) { this.reads++; samples.fill(this.amplitude); }
}
class Context {
  state = "running";
  destination = {};
  analysers: Analyser[] = [];
  silent = Object.assign(new Node(), { gain: { value: 1 } });
  createAnalyser() { const node = new Analyser(); this.analysers.push(node); return node; }
  createGain() { return this.silent; }
}

test("audio levels measure bounded RMS from existing nodes at ten samples per second without audible routing", (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  const context = new Context(), microphone = new Node(), playback = new Node();
  const levels: [number, number][] = [];
  const meter = audioLevels(context as unknown as AudioContext, (input, output) => levels.push([input, output]), () => [true, true])!;
  meter.input(microphone as unknown as AudioNode); meter.output(playback as unknown as AudioNode);
  context.analysers[0].amplitude = 0.125; context.analysers[1].amplitude = 0.5;
  t.mock.timers.tick(99); assert.deepEqual(levels, []);
  t.mock.timers.tick(1); assert.deepEqual(levels, [[0.5, 1]]);
  t.mock.timers.tick(900);
  assert.equal(context.analysers[0].reads, 10); assert.equal(context.analysers[1].reads, 10);
  assert.equal(levels.length, 1, "unchanged readings do not trigger repeated UI renders");
  assert.equal(context.silent.gain.value, 0);
  assert.deepEqual([...context.silent.connections], [context.destination]);
  assert.equal(microphone.connections.has(context.destination), false);
  context.analysers[0].amplitude = 0.002; context.analysers[1].amplitude = 0;
  t.mock.timers.tick(100); assert.deepEqual(levels.at(-1), [0, 0], "silence and device noise do not invent speech");
  context.analysers[0].amplitude = NaN;
  t.mock.timers.tick(100); assert.deepEqual(levels.at(-1), [0, 0]);
  meter.close();
});

test("mute, suspension, replacement and cleanup zero levels without stopping or rerouting the original stream", (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  const context = new Context(), first = new Node(), next = new Node(), playback = new Node(), existingRoute = {};
  first.connect(existingRoute); playback.connect(existingRoute);
  const levels: [number, number][] = [];
  let input = true, output = true;
  const meter = audioLevels(context as unknown as AudioContext, (mic, speaker) => levels.push([mic, speaker]), () => [input, output])!;
  meter.input(first as unknown as AudioNode); const releasePlayback = meter.output(playback as unknown as AudioNode);
  context.analysers.forEach((analyser) => { analyser.amplitude = 0.125; });
  t.mock.timers.tick(100); assert.deepEqual(levels.at(-1), [0.5, 0.5]);
  input = false; meter.sync(); assert.deepEqual(levels.at(-1), [0, 0.5]);
  output = false; meter.sync(); assert.deepEqual(levels.at(-1), [0, 0]);
  input = output = true; t.mock.timers.tick(100); assert.deepEqual(levels.at(-1), [0.5, 0.5]);
  context.state = "suspended"; meter.sync(); assert.deepEqual(levels.at(-1), [0, 0]);
  context.state = "running"; t.mock.timers.tick(100);
  meter.input(next as unknown as AudioNode); assert.deepEqual(levels.at(-1), [0, 0.5]);
  assert.deepEqual([...first.connections], [existingRoute]);
  assert.ok(next.connections.has(context.analysers[0]));
  releasePlayback(); t.mock.timers.tick(100); assert.deepEqual(levels.at(-1), [0.5, 0]);
  assert.deepEqual([...playback.connections], [existingRoute]);
  meter.close(); meter.close();
  assert.deepEqual(levels.at(-1), [0, 0]); assert.equal(next.connections.size, 0);
  assert.equal(context.silent.connections.size, 0);
  const count = levels.length, reads = context.analysers[0].reads;
  t.mock.timers.tick(10_000);
  assert.equal(levels.length, count); assert.equal(context.analysers[0].reads, reads);
});

test("unsupported metering releases a partial graph and never makes voice creation fail", () => {
  const context = new Context();
  context.createGain = () => { throw new Error("Analysis graph unavailable"); };
  assert.equal(audioLevels(context as unknown as AudioContext, () => {}, () => [true, true]), undefined);
  assert.ok(context.analysers.every((node) => node.connections.size === 0));
  const unused = new Context();
  assert.equal(audioLevels(unused as unknown as AudioContext, undefined, () => [true, true]), undefined);
  assert.equal(unused.analysers.length, 0);
});
