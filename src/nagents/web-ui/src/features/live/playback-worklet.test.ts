import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

type Health = { type: "health"; epoch: number; receivedSamples: number; playedSamples: number; underruns: number; droppedSamples: number; bufferedMs: number; peakBufferedMs: number; targetMs: number; sampleRate: number; correctionPpm: number; renderedAt: number; lastUnderrunAt: number; lastRecoveryAt: number; latencyRecoveries: number; interruptedSamples: number; silenceRefills: number };
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
    realtime(value = true, suppliedEpoch = epoch) { processor.port.onmessage({ data: { type: "mode", epoch: suppliedEpoch, realtime: value } }); },
    barrier() { epoch++; processor.port.onmessage({ data: { type: "barrier", epoch } }); },
    get time() { return position / rate; },
    push(buffer: ArrayBuffer, suppliedEpoch = epoch) { globals.currentTime = position / rate; processor.port.onmessage({ data: { type: "pcm", buffer, epoch: suppliedEpoch } }); },
    reset() { epoch++; processor.port.onmessage({ data: { type: "reset", epoch } }); },
    stats() { processor.port.onmessage({ data: { type: "health" } }); return health.at(-1)!; },
  };
}
function pcm(size = 480, value = 8192) { const samples = new Int16Array(size); samples.fill(value); return samples.buffer; }
function voicedPcm(offset: number, size = 480) {
  const samples = new Int16Array(size);
  for (let i = 0; i < size; i++) {
    const time = (offset + i) / 24000;
    const envelope = .45 + .2 * Math.sin(2 * Math.PI * 3 * time);
    samples[i] = Math.round(16384 * envelope * (Math.sin(2 * Math.PI * 167 * time) + .25 * Math.sin(2 * Math.PI * 835 * time)));
  }
  return samples.buffer;
}

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

for (const rate of [44100, 48000]) for (const gapMs of [70, 108, 150]) test(`repeated ${gapMs}ms delivery gaps re-prime voiced audio once at ${rate}Hz`, () => {
  const f = fixture(rate), gap = gapMs / 1000;
  let packet = 0, laterSilentBlocks = 0, firstAudible = -1;
  // Six seconds of continuous voiced PCM: each one-second boundary stalls a
  // batch, which then catches up without deleting, duplicating or reordering it.
  const arrival = (index: number) => {
    const nominal = index * .02, second = Math.floor(index / 50);
    return second >= 1 ? Math.max(nominal, second + gap - .02) : nominal;
  };
  while (f.time < 5.8) {
    while (packet < 300 && arrival(packet) <= f.time) { f.push(voicedPcm(packet * 480)); packet++; }
    const output = f.render();
    if (firstAudible < 0 && output.some(value => Math.abs(value) > .01)) firstAudible = f.time;
    if (f.time > 1.6 && output.every(value => value === 0)) laterSilentBlocks++;
  }
  const active = f.stats();
  assert(firstAudible > 0 && firstAudible < .05, "clean initial audio must retain its small startup reserve");
  assert.equal(active.underruns, 1, "the first unexpected gap may starve; subsequent identical gaps must be absorbed");
  assert.equal(laterSilentBlocks, 0, "voiced samples must stay continuous after the first recovery, before EOF");
  assert(active.targetMs >= gapMs && active.targetMs <= 160, "reserve must learn the peak quickly within the latency cap");
  assert(active.peakBufferedMs < 190); assert.equal(active.droppedSamples, 0);
  while (f.time < 7) {
    while (packet < 300 && arrival(packet) <= f.time) { f.push(voicedPcm(packet * 480)); packet++; }
    f.render();
  }
  const ended = f.stats();
  assert.equal(ended.underruns, 2, "EOF is separate from the one initial delivery starvation");
  assert.equal(ended.receivedSamples, 300 * 480);
  assert.equal(ended.playedSamples, ended.receivedSamples, "re-priming must preserve the entire voiced burst and final tail");
  assert.equal(ended.droppedSamples, 0);
});

test("a learned jitter reserve falls slowly and ignores a long pause between replies", () => {
  const f = fixture(); f.push(voicedPcm(0));
  while (f.time < .14) f.render();
  f.push(voicedPcm(480));
  const learned = f.stats().targetMs;
  assert(learned >= 155 && learned <= 160);
  let packet = 2;
  while (f.time < 3) {
    while (.14 + (packet - 1) * .02 <= f.time) { f.push(voicedPcm(packet * 480)); packet++; }
    f.render();
  }
  const relaxed = f.stats().targetMs;
  assert(relaxed < learned && relaxed > learned - 7, "a few clean packets must not erase the learned reserve");
  while (f.time < 7) f.render();
  const before = f.stats();
  f.push(pcm());
  assert(f.stats().targetMs < before.targetMs, "a long conversational pause must not inflate jitter reserve");
  while (f.time < 7.2) f.render();
  assert.equal(f.stats().playedSamples, f.stats().receivedSamples, "a short final packet still plays within the bounded deadline");
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

test("real-time playback bounds age through six minutes of repeated delivery stalls", () => {
  const f = fixture(); f.realtime();
  let packet = 0, checked = 0, oldest = 0;
  // Each sample encodes its source frame. The audio thread keeps rendering
  // while a blocked UI delivers several old packets together every four seconds.
  const arrival = (index: number) => {
    const nominal = index * .02, boundary = Math.floor(nominal / 4) * 4;
    return boundary ? Math.max(nominal, boundary + .35) : nominal;
  };
  while (f.time < 360) {
    while (arrival(packet) <= f.time) { f.push(pcm(480, packet + 1000)); packet++; }
    const output = f.render();
    const stats = f.stats();
    assert(stats.bufferedMs <= 320 && stats.peakBufferedMs <= 320);
    // Ignore the short recovery fade and silence during the forced outage.
    if (stats.playedSamples > 480 && output.at(-1)! > .02 &&
        f.time - stats.lastRecoveryAt > .012 && f.time - stats.lastUnderrunAt > .012) {
      const sourceFrame = output.at(-1)! * 32768 - 1000;
      const age = f.time - sourceFrame * .02;
      oldest = Math.max(oldest, age); checked++;
      assert(age < .37, `real-time speech fell ${age}s behind at ${f.time}s`);
    }
    f.played.length = 0; f.health.length = 0;
  }
  const stats = f.stats();
  assert(checked > 1000 && oldest > .04);
  assert(stats.latencyRecoveries > 50 && stats.droppedSamples > 0);
  assert.equal(stats.receivedSamples, stats.playedSamples + stats.droppedSamples + Math.round(stats.bufferedMs * 24));
});

test("real-time silence replenishes a learned reserve before speech without discarding or stretching it", () => {
  const f = fixture(); f.realtime();
  const rendered: number[] = [];
  let packet = 0;
  // A 1% delivery shortfall and two permanent transport delays: learn the
  // observed 217ms gap in silence, then absorb a smaller gap during speech.
  const arrival = (index: number) => index * .0202 + (index >= 100 ? .217 : 0) + (index >= 500 ? .14 : 0);
  while (f.time < 19) {
    while (packet < 900 && arrival(packet) <= f.time) {
      f.push(pcm(480, packet >= 300 && packet < 700 ? 8192 : 0)); packet++;
    }
    rendered.push(...f.render());
  }
  const first = rendered.findIndex(value => value > .1);
  const last = rendered.findLastIndex(value => value > .1);
  assert(first > 0 && last > first);
  assert(rendered.slice(first, last).every(value => value > .1), "a learned delivery gap must not cut silence into voiced PCM");
  assert(Math.abs((last - first) / 48000 - 8) < .02, "voiced duration is preserved; silence refill does not stretch speech");
  const stats = f.stats();
  assert(stats.silenceRefills > 0);
  assert.equal(stats.droppedSamples, 0); assert.equal(stats.latencyRecoveries, 0);
  assert(stats.peakBufferedMs <= 320);
  assert.equal(stats.receivedSamples, stats.playedSamples + Math.round(stats.bufferedMs * 24));
});

test("only explicitly selected real-time mode discards a stale incoming burst", () => {
  const buffered = fixture(); buffered.push(pcm(12000));
  assert.equal(buffered.stats().bufferedMs, 500); assert.equal(buffered.stats().droppedSamples, 0);
  const realtime = fixture(); realtime.realtime(true, 8); realtime.push(pcm(12000));
  assert.equal(realtime.stats().bufferedMs, 500, "stale mode commands cannot reconfigure playback");
  realtime.reset(); realtime.realtime(); realtime.push(pcm(24000));
  const stats = realtime.stats();
  assert.equal(stats.bufferedMs, 80); assert.equal(stats.latencyRecoveries, 1);
  assert.equal(stats.droppedSamples, 22080); assert.equal(stats.lastRecoveryAt, 0);
  assert.equal(stats.interruptedSamples, 12000, "resets account for queued samples separately from overload drops");
});

test("audio-thread health carries the actual starvation time", () => {
  const f = fixture(); f.push(pcm(2400));
  while (f.time < .3) f.render();
  const stats = f.stats();
  assert.equal(stats.underruns, 1);
  assert(stats.lastUnderrunAt >= .1 && stats.lastUnderrunAt < .2);
  assert(stats.renderedAt <= f.time && f.time - stats.renderedAt < .003);
  while (f.time < .8) f.render();
  assert.equal(f.stats().lastUnderrunAt, stats.lastUnderrunAt, "polling must not move an event timestamp");
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
