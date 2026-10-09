import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

type Health = { type: "health"; epoch: number; receivedSamples: number; playedSamples: number; underruns: number; droppedSamples: number; bufferedMs: number; peakBufferedMs: number; targetMs: number; sampleRate: number; correctionPpm: number };
type Played = { type: "played"; epoch: number; at: number; buffer: ArrayBuffer };
function fixture(rate = 48000) {
  let Processor!: new () => { port: { onmessage(event: { data: object }): void }; process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean };
  const health: Health[] = [], played: Played[] = [];
  let acknowledge = true;
  class Base { port = { onmessage: (_event: { data: object }) => {}, postMessage(data: Health | Played) { if (data.type === "health") health.push(data); else { played.push(data); if (acknowledge) processor.port.onmessage({ data: { type: "played-ack", epoch: data.epoch } }); } } }; }
  const globals = { AudioWorkletProcessor: Base, sampleRate: rate, currentTime: 0, ArrayBuffer, Int16Array, Float64Array, DataView, Number, Math,
    registerProcessor(name: string, type: typeof Processor) { assert.equal(name, "ngn-live-playback"); Processor = type; } };
  runInNewContext(readFileSync(new URL("../../../public/assets/live-playback.js", import.meta.url), "utf8"), globals);
  const processor = new Processor();
  let position = 0, epoch = 0;
  const output = new Float32Array(128);
  const render = () => {
    globals.currentTime = position / rate;
    processor.process([], [[output]]); position += output.length;
    return output;
  };
  return { health, played, render, acknowledge: (value: boolean) => { acknowledge = value; },
    barrier() { epoch++; processor.port.onmessage({ data: { type: "barrier", epoch } }); },
    get time() { return position / rate; },
    push(buffer: ArrayBuffer, suppliedEpoch = epoch) { globals.currentTime = position / rate; processor.port.onmessage({ data: { type: "pcm", buffer, epoch: suppliedEpoch } }); },
    reset() { epoch++; processor.port.onmessage({ data: { type: "reset", epoch } }); },
    stats() { processor.port.onmessage({ data: { type: "health" } }); return health.at(-1)!; },
  };
}
function pcm(size = 480, value = 8192) { const samples = new Int16Array(size); samples.fill(value); return samples.buffer; }

for (const rate of [16000, 24000, 44100, 48000, 96000]) test(`continuous PCM playout resamples 24kHz at ${rate}Hz without packet-boundary gaps`, () => {
  const f = fixture(rate);
  let packet = 0, audible = 0, zeroAfterStart = 0;
  while (f.time < 3) {
    while (packet * .02 <= f.time) { f.push(pcm()); packet++; }
    for (const value of f.render()) {
      assert(Number.isFinite(value) && Math.abs(value) <= 1);
      if (value > .24) audible++;
      else if (audible && value === 0) zeroAfterStart++;
    }
  }
  const stats = f.stats();
  assert.equal(stats.underruns, 0); assert.equal(stats.droppedSamples, 0);
  assert.equal(zeroAfterStart, 0); assert(audible > rate * 2.8); assert(stats.bufferedMs < 80);
  assert(f.played.length > 100);
  assert(f.played.every(frame => frame.epoch === 0 && frame.buffer.byteLength === 960));
  for (let i = 1; i < f.played.length; i++) assert(Math.abs(f.played[i].at - f.played[i - 1].at - .02) < .0001);
});

test("a delayed main-thread delivery batch does not create holes when the audio thread has buffered PCM", () => {
  const f = fixture();
  for (let i = 0; i < 4; i++) f.push(pcm());
  let packet = 4, started = false, holes = 0;
  while (f.time < 4) {
    const arrival = packet * .02 - .04 + (packet % 6 === 0 ? .012 : 0);
    if (arrival <= f.time) { f.push(pcm()); packet++; }
    for (const value of f.render()) {
      if (value > .24) started = true;
      if (started && value === 0) holes++;
    }
  }
  assert.equal(holes, 0); assert.equal(f.stats().underruns, 0);
  assert(f.stats().targetMs >= 40 && f.stats().targetMs <= 160);
});

test("starvation and overload are bounded and fade discontinuities instead of stopping queued nodes", () => {
  const f = fixture(); f.push(pcm(2400));
  while (f.time < .3) f.render();
  assert.equal(f.stats().underruns, 1);
  f.push(pcm(24000, -8192)); f.push(pcm(24000, 8192));
  const stats = f.stats();
  assert(stats.droppedSamples >= 24000); assert(stats.bufferedMs <= 1000); assert(stats.peakBufferedMs <= 1000);
  let previous = 0;
  for (let i = 0; i < 20; i++) for (const value of f.render()) {
    assert(Math.abs(value - previous) < .02); previous = value;
  }
});

test("reset epochs discard stale queued PCM and stale post-reset transfers", () => {
  const f = fixture(); f.push(pcm(24000)); f.render(); f.reset(); f.push(pcm(4800), 0);
  for (let i = 0; i < 100; i++) assert(f.render().every(value => value === 0));
  assert.equal(f.stats().bufferedMs, 0);
  f.push(pcm(960)); for (let i = 0; i < 12; i++) f.render();
  assert(f.played.some(frame => frame.epoch === 1));
});

for (const speed of [.9997, 1.0003]) test(`independent ${speed} source and device clocks stay bounded for a 190-second call`, () => {
  const f = fixture(); let packet = 0;
  while (f.time < 190) {
    while (packet * .02 / speed <= f.time) { f.push(pcm()); packet++; }
    f.render(); f.played.length = 0;
  }
  const stats = f.stats();
  assert.equal(stats.underruns, 0); assert.equal(stats.droppedSamples, 0);
  assert(stats.bufferedMs < 100); assert(Math.abs(stats.correctionPpm) <= 1000.001);
});


test("nonintegral tone phases stay continuous across variable provider packet boundaries", () => {
  const f = fixture(); let sent = 0, next = 0, previous = 0, largest = 0;
  while (f.time < 2) {
    if (f.time >= next) {
      const size = sent ? 480 : 464, wave = new Int16Array(size);
      for (let i = 0; i < size; i++) wave[i] = Math.round(Math.sin(2 * Math.PI * 997 * (sent + i) / 24000) * 16384);
      f.push(wave.buffer); sent += size; next += size / 24000;
    }
    for (const value of f.render()) { largest = Math.max(largest, Math.abs(value - previous)); previous = value; }
  }
  assert(largest < .075, `packet boundaries introduced a sample jump of ${largest}`);
  assert.equal(f.stats().underruns, 0);
});

test("low-rate output filters ultrasonic source content instead of aliasing it into speech", () => {
  function energy(frequency: number) {
    const f = fixture(16000); let sent = 0, sum = 0, count = 0;
    while (f.time < 1) {
      while (sent / 24000 <= f.time) {
        const wave = new Int16Array(480);
        for (let i = 0; i < wave.length; i++) wave[i] = Math.round(Math.sin(2 * Math.PI * frequency * (sent + i) / 24000) * 16384);
        f.push(wave.buffer); sent += wave.length;
      }
      const output = f.render();
      if (f.time > .2) for (const value of output) { sum += value * value; count++; }
    }
    return Math.sqrt(sum / count);
  }
  assert(energy(1000) > .3);
  assert(energy(10000) < .01, "10kHz must not alias to an audible 6kHz tone on a 16kHz device");
});

test("health telemetry cannot grow an unbounded message queue when the UI is stalled", () => {
  const f = fixture();
  while (f.time < 20) f.render();
  assert.equal(f.health.length, 1, "the worklet waits for an acknowledgement before its next periodic report");
});


test("a fresh reply re-primes after silence instead of reusing the old starvation deadline", () => {
  const f = fixture(); f.push(pcm());
  while (f.time < 1) f.render();
  assert.equal(f.stats().bufferedMs, 0);
  f.push(pcm());
  assert(f.render().every(value => value === 0), "a late new packet needs its own buffer deadline");
  while (f.time < 1.08) f.render();
  assert(f.stats().playedSamples >= 960, "the final sample is preserved, including short replies");
});

test("played PCM reports are credit-bounded and report barriers preserve audible samples", () => {
  const f = fixture(); f.acknowledge(false); f.push(pcm(24000));
  while (f.time < .3) f.render();
  assert.equal(f.played.length, 1);
  const before = f.stats(); f.barrier();
  assert.equal(f.stats().bufferedMs, before.bufferedMs);
  assert(f.render().every(value => value > .24));
  while (f.time < .6) f.render();
  assert.equal(f.played.length, 2, "a barrier permits just one new-generation report");
  assert.equal(f.played.at(-1)?.epoch, 1);
  assert.equal(f.stats().droppedSamples, 0);
});
