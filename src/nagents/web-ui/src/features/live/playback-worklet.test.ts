import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

type Health = { type: "health"; epoch: number; receivedSamples: number; playedSamples: number; underruns: number; droppedSamples: number; bufferedMs: number; peakBufferedMs: number; targetMs: number; sampleRate: number; correctionPpm: number; renderedAt: number; lastUnderrunAt: number; lastRecoveryAt: number; latencyRecoveries: number; interruptedSamples: number; silenceRefills: number };
type Played = { type: "played"; epoch: number; at: number; until: number; sourceStart: number; buffer: ArrayBuffer };
type Trace = { type: "trace"; epoch: number; sequence: number; omittedIntervals: number; intervals: { sourceStart: number; samples: number; at: number; until: number }[] };
type TraceCheckpoint = { type: "trace-checkpoint"; epoch: number; requestId: number; status: "completed" | "interrupted" | "busy"; acceptedEnd: number; renderedEnd: number; receivedEnd: number; renderedAt: number; omittedIntervals: number };
function fixture(rate = 48000) {
  let Processor!: new () => { port: { onmessage(event: { data: object }): void }; process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean };
  const health: Health[] = [], played: Played[] = [], traces: Trace[] = [], checkpoints: TraceCheckpoint[] = [];
  let acknowledge = true, acknowledgeTrace = true;
  class Base { port = { onmessage: (_event: { data: object }) => {}, postMessage(data: Health | Played | Trace | TraceCheckpoint) {
    if (data.type === "health") health.push(data);
    else if (data.type === "trace") {
      traces.push(data);
      if (acknowledgeTrace) processor.port.onmessage({ data: { type: "trace-ack", epoch: data.epoch, sequence: data.sequence } });
    } else if (data.type === "trace-checkpoint") checkpoints.push(data);
    else {
      played.push(data);
      if (acknowledge) processor.port.onmessage({ data: { type: "played-ack", epoch: data.epoch } });
    }
  } }; }
  const globals = { AudioWorkletProcessor: Base, sampleRate: rate, currentTime: 0, ArrayBuffer, Int16Array, Float64Array, DataView, Number, Math,
    registerProcessor(name: string, type: typeof Processor) { assert.equal(name, "ngn-live-playback"); Processor = type; } };
  runInNewContext(readFileSync(new URL("../../../public/assets/live-playback.js", import.meta.url), "utf8"), globals);
  const processor = new Processor();
  let position = 0, epoch = 0;
  const output = new Float32Array(128);
  const renderAt = (currentTime: number) => {
    globals.currentTime = currentTime;
    processor.process([], [[output]]); position += output.length;
    return output;
  };
  const render = () => renderAt(position / rate);
  return { health, played, traces, checkpoints, render, acknowledge: (value: boolean) => { acknowledge = value; },
    acknowledgeTrace(value: boolean) { acknowledgeTrace = value; },
    trace(enabled = true, suppliedEpoch = epoch) { processor.port.onmessage({ data: { type: "trace", enabled, epoch: suppliedEpoch } }); },
    traceAck(sequence: number, suppliedEpoch = epoch) { processor.port.onmessage({ data: { type: "trace-ack", sequence, epoch: suppliedEpoch } }); },
    traceCheckpoint(requestId: number, suppliedEpoch = epoch) { processor.port.onmessage({ data: { type: "trace-checkpoint", requestId, epoch: suppliedEpoch } }); },
    realtime(value = true, suppliedEpoch = epoch) { processor.port.onmessage({ data: { type: "mode", epoch: suppliedEpoch, realtime: value } }); },
    renderAt,
    barrier() { epoch++; processor.port.onmessage({ data: { type: "barrier", epoch } }); },
    get time() { return position / rate; },
    push(buffer: ArrayBuffer, suppliedEpoch = epoch) { globals.currentTime = position / rate; processor.port.onmessage({ data: { type: "pcm", buffer, epoch: suppliedEpoch } }); },
    reset() { epoch++; processor.port.onmessage({ data: { type: "reset", epoch } }); },
    stats() { processor.port.onmessage({ data: { type: "health" } }); return health.at(-1)!; },
  };
}
function pcm(size = 480, value = 8192) { const samples = new Int16Array(size); samples.fill(value); return samples.buffer; }
function voicedThenQuietPcm(voicedSamples = 780, totalSamples = 960) {
  const samples = new Int16Array(totalSamples);
  samples.fill(8192, 0, voicedSamples);
  samples.fill(100, voicedSamples);
  return samples.buffer;
}
function voicedPcm(offset: number, size = 480) {
  const samples = new Int16Array(size);
  for (let i = 0; i < size; i++) {
    const time = (offset + i) / 24000;
    const envelope = .45 + .2 * Math.sin(2 * Math.PI * 3 * time);
    samples[i] = Math.round(16384 * envelope * (Math.sin(2 * Math.PI * 167 * time) + .25 * Math.sin(2 * Math.PI * 835 * time)));
  }
  return samples.buffer;
}

test("played reports identify the original PCM samples and their render interval", () => {
  const f = fixture(), source = new Int16Array(4800);
  for (let i = 0; i < source.length; i++) source[i] = i - 2400;
  f.push(source.buffer);
  while (f.time < .25) f.render();
  assert.equal(f.played.length, 10);
  for (const [index, report] of f.played.entries()) {
    assert.equal(report.sourceStart, index * 480);
    assert.deepEqual(new Int16Array(report.buffer), source.slice(report.sourceStart, report.sourceStart + 480));
    assert(Math.abs(report.until - report.at - 479 / 24000) < .0001);
  }
});

test("source coordinates account for interrupted and overloaded input without treating it as rendered", () => {
  const reset = fixture(); reset.push(pcm(2400));
  while (reset.time < .03) reset.render();
  reset.reset(); reset.push(pcm(2400, -8192));
  while (reset.time < .2) reset.render();
  const next = reset.played.find(report => report.epoch === 1)!;
  assert.equal(next.sourceStart, 2400);
  assert(new Int16Array(next.buffer).every(value => value === -8192));
  assert(reset.stats().interruptedSamples > 0);

  const overload = fixture(); overload.push(pcm(24000)); overload.push(pcm(24000, -8192));
  while (overload.time < .05) overload.render();
  assert.equal(overload.played[0].sourceStart, 24000);
  assert.equal(overload.stats().droppedSamples, 24000);
  assert(new Int16Array(overload.played[0].buffer).every(value => value === -8192));
});

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
  const f = fixture(); f.realtime(); f.trace();
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
  const intervals = f.traces.flatMap(batch => batch.intervals);
  let end = 0, sawPartialReport = false;
  for (const interval of intervals) {
    assert.equal(interval.sourceStart, end, "refill flushes must preserve contiguous source coordinates");
    const nominalSpan = (interval.samples - 1) / 24000;
    assert(Math.abs(interval.until - interval.at - nominalSpan) <= nominalSpan * .001 + 128 / 48000,
      "render timing must not bridge a silence refill or overload discard");
    if (interval.samples < 480) sawPartialReport = true;
    end += interval.samples;
  }
  assert(sawPartialReport, "the silence refill should flush a partial played interval");
});

function learnedReserveBeforeUnderflow(quietTail: boolean) {
  const f = fixture(); f.realtime();
  for (let i = 0; i < 15; i++) f.push(pcm(480));
  while (f.time < .17) f.render();
  // A long but non-starving arrival interval teaches a substantial reserve.
  for (let i = 0; i < 4; i++) f.push(pcm(480));
  while (f.time < .33) f.render();
  f.push(quietTail ? voicedThenQuietPcm(780, 960) : pcm(960));
  while (f.stats().underruns === 0 && f.time < .6) f.render();
  const underrunAt = f.stats().lastUnderrunAt;
  assert(underrunAt > 0, "the prepared queue should eventually starve");
  assert(f.stats().targetMs >= 180, "the setup must learn a substantial ordinary reserve");
  return { f, underrunAt };
}

test("a short quiet-tail underflow uses a fast one-shot prime without losing PCM", () => {
  const { f, underrunAt } = learnedReserveBeforeUnderflow(true);
  let packet = 0, firstAudibleAfterRecovery = -1;
  while (f.time < underrunAt + .2) {
    if (packet === 0 && f.time >= underrunAt + .01) { f.push(pcm(480)); packet++; }
    if (packet === 1 && f.time >= underrunAt + .03) { f.push(pcm(480)); packet++; }
    const output = f.render();
    if (packet > 0 && firstAudibleAfterRecovery < 0 && output.some(value => Math.abs(value) > .01)) {
      firstAudibleAfterRecovery = f.time;
    }
  }
  assert(firstAudibleAfterRecovery > underrunAt);
  assert(firstAudibleAfterRecovery - underrunAt < .05,
    "a quiet tail and arrival within 100ms should use the one-shot 40ms prime");
  assert(f.stats().targetMs >= 180, "fast recovery must not lower the learned normal target");
  while (f.time < 1) f.render();
  const done = f.stats();
  assert.equal(done.droppedSamples, 0);
  assert.equal(done.playedSamples, done.receivedSamples, "fast recovery preserves the full PCM stream");
});

test("a voiced tail keeps the learned reserve when the next packet arrives within 100ms", () => {
  const { f, underrunAt } = learnedReserveBeforeUnderflow(false);
  let firstArrival = -1, firstResume = -1, nextPacketAt = underrunAt + .01;
  while (f.time < underrunAt + .65) {
    if (f.time >= nextPacketAt) {
      if (firstArrival < 0) firstArrival = f.time;
      f.push(pcm(480));
      nextPacketAt += .02;
    }
    const output = f.render();
    if (firstArrival >= 0 && firstResume < 0 && output.some(value => Math.abs(value) > .01)) firstResume = f.time;
  }
  assert(firstArrival - underrunAt < .1, "the voiced-tail case must be inside the fast-arrival window");
  assert(firstResume - firstArrival > .16,
    `voiced tails must use the full learned reserve (waited ${firstResume - firstArrival}s)`);
  assert(f.stats().targetMs >= 180);
  assert.equal(f.stats().droppedSamples, 0);
});

test("a quiet-tail recovery arriving after 100ms uses the learned reserve", () => {
  const { f, underrunAt } = learnedReserveBeforeUnderflow(true);
  let firstArrival = -1, firstResume = -1, nextPacketAt = underrunAt + .12;
  while (f.time < underrunAt + .7) {
    if (f.time >= nextPacketAt) {
      if (firstArrival < 0) firstArrival = f.time;
      f.push(pcm(480));
      nextPacketAt += .02;
    }
    const output = f.render();
    if (firstArrival >= 0 && firstResume < 0 && output.some(value => Math.abs(value) > .01)) firstResume = f.time;
  }
  assert(firstArrival - underrunAt > .1, "the arrival is outside the fast-recovery window");
  assert(firstResume - firstArrival > .1, "late arrivals must retain the ordinary learned prime");
  assert(f.stats().targetMs >= 180);
  assert.equal(f.stats().droppedSamples, 0);
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
  assert(f.played[1].sourceStart > f.played[0].sourceStart + 480,
    "skipped visual reports must retain their true position in the input stream");
  assert.equal(f.stats().droppedSamples, 0);
});

test("numeric timing trace is opt-in and preserves output when visual reports are skipped", () => {
  const plain = fixture(), traced = fixture();
  plain.acknowledge(false); traced.acknowledge(false); traced.trace();
  let packet = 0;
  while (plain.time < 2.3) {
    while (packet < 100 && packet * .02 <= plain.time) {
      plain.push(voicedPcm(packet * 480)); traced.push(voicedPcm(packet * 480)); packet++;
    }
    assert.deepEqual(traced.render(), plain.render(), "diagnostics must not change audible PCM or timing");
  }
  assert.equal(plain.traces.length, 0);
  assert.equal(traced.played.length, 1, "the visual report consumer deliberately stops acknowledging");
  const intervals = traced.traces.flatMap(batch => batch.intervals);
  let end = 0;
  for (const [sequence, batch] of traced.traces.entries()) {
    assert.equal(batch.sequence, sequence); assert.equal(batch.epoch, 0);
    assert.equal(batch.omittedIntervals, 0);
    assert(batch.intervals.length > 0 && batch.intervals.length <= 10);
  }
  for (const interval of intervals) {
    assert.equal(interval.sourceStart, end);
    assert(interval.at >= 0 && interval.until >= interval.at);
    assert(Math.abs(interval.until - interval.at - (interval.samples - 1) / 24000) < .0001);
    end += interval.samples;
  }
  assert.equal(end, 48000, "every played source sample has timing evidence, including the final tail");
  assert.equal(traced.stats().playedSamples, end);
  assert.equal(traced.stats().droppedSamples, 0);
  assert(traced.traces.length < 20, "small numeric intervals are batched rather than sent for every packet");
});

test("trace intervals flush at repeated and caught-up render-clock quanta without changing PCM", () => {
  const plain = fixture(), discontinuous = fixture(), ordinaryTrace = fixture();
  discontinuous.trace(); ordinaryTrace.trace();
  const source = voicedPcm(0, 4800);
  plain.push(source.slice(0)); discontinuous.push(source.slice(0)); ordinaryTrace.push(source.slice(0));
  let quantum = 0;
  while (plain.stats().playedSamples < 4800 && quantum < 100) {
    const expected = plain.render();
    const irregular = quantum === 1
      ? discontinuous.renderAt(0) // Repeated currentTime: this quantum should start a new report.
      : quantum === 2
        ? discontinuous.renderAt(2 * 128 / 48000) // Caught-up clock: flush before consuming this quantum too.
        : discontinuous.render();
    const regular = ordinaryTrace.render();
    assert.deepEqual(irregular, expected, "clock telemetry cannot change rendered float PCM");
    assert.deepEqual(regular, expected, "ordinary trace timing cannot change rendered float PCM");
    quantum++;
  }
  assert(quantum < 100);
  discontinuous.traceCheckpoint(41); ordinaryTrace.traceCheckpoint(42);
  assert.equal(discontinuous.checkpoints[0]?.status, "completed");
  assert.equal(ordinaryTrace.checkpoints[0]?.status, "completed");

  const collect = (f: ReturnType<typeof fixture>) => Int16Array.from(
    f.played.flatMap(report => Array.from(new Int16Array(report.buffer))),
  );
  const expectedPcm = collect(plain);
  assert.deepEqual(collect(discontinuous), expectedPcm, "clock discontinuities must not alter captured output samples");
  assert.deepEqual(collect(ordinaryTrace), expectedPcm, "ordinary tracing must retain byte-identical played PCM");

  const intervals = discontinuous.traces.flatMap(batch => batch.intervals);
  let sourceEnd = 0;
  for (const interval of intervals) {
    assert.equal(interval.sourceStart, sourceEnd, "flushes preserve exact source coverage without overlaps");
    const expectedSpan = (interval.samples - 1) / 24000;
    assert(Math.abs(interval.until - interval.at - expectedSpan) <= expectedSpan * .001 + 1 / 48000,
      "no trace interval may stretch across a repeated/caught-up render-clock boundary");
    sourceEnd += interval.samples;
  }
  assert.equal(sourceEnd, 4800);

  const ordinaryIntervals = ordinaryTrace.traces.flatMap(batch => batch.intervals);
  assert(ordinaryIntervals.length > 0);
  assert(ordinaryIntervals.every(interval => interval.samples === 480),
    "a stable render clock keeps its ordinary full-size interval batching");
});

test("a stalled timing observer has one in-flight batch and bounded explicitly incomplete evidence", () => {
  const f = fixture(); f.trace(); f.acknowledgeTrace(false);
  let packet = 0;
  while (f.time < 4.3) {
    while (packet < 200 && packet * .02 <= f.time) { f.push(pcm()); packet++; }
    f.render();
  }
  assert.equal(f.traces.length, 1, "a stalled diagnostic consumer cannot enqueue unbounded messages");
  assert.equal(f.stats().playedSamples, 96000, "diagnostic backpressure never blocks audible PCM");
  assert.equal(f.stats().droppedSamples, 0);
  f.traceAck(99);
  assert.equal(f.traces.length, 1, "an unrelated acknowledgement must not release the in-flight batch");
  f.acknowledgeTrace(true); f.traceAck(0);
  while (f.time < 4.6) f.render();
  const intervals = f.traces.flatMap(batch => batch.intervals);
  assert(intervals.length <= 65, "at most 64 intervals wait behind the one in-flight interval");
  assert.equal(f.traces.at(-1)?.omittedIntervals, 200 - intervals.length,
    "all omitted timing intervals are explicit; the recording cannot falsely claim complete coverage");
});

test("timing epochs reject stale acknowledgements without resetting playback or enabling tracing by accident", () => {
  const f = fixture(); f.trace(true, 9); f.push(pcm(2400));
  while (f.time < .2) f.render();
  assert.equal(f.traces.length, 0, "a stale enable message is ignored");
  f.trace(); f.acknowledgeTrace(false); f.push(pcm(2400));
  while (f.time < .4) f.render();
  assert.equal(f.traces.length, 1);
  f.reset(); f.push(pcm(2400));
  while (f.time < .6) f.render();
  assert.equal(f.traces.length, 2);
  assert.equal(f.traces[1].epoch, 1); assert.equal(f.traces[1].sequence, 0);
  f.traceAck(0, 0);
  while (f.time < .8) f.render();
  assert.equal(f.traces.length, 2, "an old epoch cannot acknowledge the new epoch's batch");
  f.traceAck(0); f.acknowledgeTrace(true);
  while (f.time < 1.1) f.render();
  assert(f.traces.length > 2, "the matching acknowledgement releases queued evidence");
  assert.equal(f.stats().droppedSamples, 0);
});


test("same-epoch trace disable and re-enable cannot accept an old acknowledgement", () => {
  const f = fixture(); f.trace(); f.acknowledgeTrace(false); f.push(pcm(4800));
  while (f.time < .3) f.render();
  assert.equal(f.traces.length, 1);
  assert.equal(f.traces[0].sequence, 0);

  f.trace(false); f.trace(true); f.push(pcm(4800));
  while (f.time < .6 && f.traces.length < 2) f.render();
  assert.equal(f.traces.length, 2);
  assert.equal(f.traces[1].sequence, 1, "same-epoch sequence numbers remain unique across toggles");
  f.traceAck(0);
  while (f.time < .8) f.render();
  assert.equal(f.traces.length, 2, "a late pre-disable acknowledgement cannot release the new batch");
  f.traceAck(1);
  while (f.time < 1) f.render();
  assert.equal(f.traces.length, 3, "the matching acknowledgement releases newly queued intervals");
  assert.equal(f.traces[2].sequence, 2);
  assert.equal(f.stats().droppedSamples, 0);
});


test("trace checkpoints snapshot accepted PCM while production continues and drain exact prefix timing", () => {
  const plain = fixture(), traced = fixture(); traced.trace();
  plain.push(pcm(2400)); traced.push(pcm(2400));
  traced.traceCheckpoint(17);
  traced.traceCheckpoint(18);
  assert.equal(traced.checkpoints.length, 1);
  assert.equal(traced.checkpoints[0].status, "busy");
  assert.equal(traced.checkpoints[0].requestId, 18);
  let futurePacket = false;
  while (!traced.checkpoints.some(item => item.requestId === 17)) {
    if (!futurePacket && traced.time >= .05) {
      plain.push(pcm(480)); traced.push(pcm(480)); futurePacket = true;
    }
    assert.deepEqual(traced.render(), plain.render(), "checkpoints and trace metadata must not change PCM");
  }
  const checkpoint = traced.checkpoints.find(item => item.requestId === 17)!;
  assert.equal(checkpoint.status, "completed");
  assert.equal(checkpoint.acceptedEnd, 2400);
  assert(checkpoint.receivedEnd > checkpoint.acceptedEnd, "the producer continues after the accepted snapshot");
  assert(checkpoint.renderedEnd >= checkpoint.acceptedEnd);
  assert(checkpoint.renderedEnd - checkpoint.acceptedEnd <= 128, "completion may overshoot by at most one output quantum");
  assert.equal(checkpoint.renderedAt, traced.time);
  const intervals = traced.traces.flatMap(batch => batch.intervals);
  let end = 0;
  for (const interval of intervals) {
    assert.equal(interval.sourceStart, end);
    end += interval.samples;
  }
  assert(end >= checkpoint.renderedEnd, "the completed checkpoint flushes its partial timing interval");
  assert.equal(checkpoint.omittedIntervals, 0);
});

test("an empty trace checkpoint completes immediately and tracing remains opt-in", () => {
  const disabled = fixture(); disabled.traceCheckpoint(1);
  assert.equal(disabled.checkpoints.length, 0, "normal playback ignores checkpoint requests unless tracing is enabled");
  const f = fixture(); f.trace(); f.traceCheckpoint(2);
  assert.equal(JSON.stringify(f.checkpoints), JSON.stringify([{ type: "trace-checkpoint", epoch: 0, requestId: 2, status: "completed",
    acceptedEnd: 0, renderedEnd: 0, receivedEnd: 0, renderedAt: 0, omittedIntervals: 0 }]));
});

test("a completed checkpoint force-drains small queued batches after the matching trace acknowledgement", () => {
  const f = fixture(); f.trace(); f.acknowledgeTrace(false); f.push(pcm(4800));
  while (f.time < .24) f.render();
  assert.equal(f.traces.length, 1);
  f.traceCheckpoint(23);
  while (!f.checkpoints.length && f.time < .45) f.render();
  assert.equal(f.checkpoints[0]?.status, "completed");
  assert.equal(f.traces.length, 1, "the in-flight batch still requires its acknowledgement");
  f.traceAck(0);
  assert.equal(f.traces.length, 2, "force-drain sends the queued partial batch immediately");
  assert(f.traces[1].intervals.length < 10);
  assert.equal(f.traces[1].sequence, 1);
  assert.equal(f.stats().droppedSamples, 0);
});

test("reset, barrier, and overflow interrupt pending trace checkpoints without false completion", () => {
  for (const boundary of ["reset", "barrier"] as const) {
    const f = fixture(); f.trace(); f.push(pcm(4800)); f.traceCheckpoint(31);
    if (boundary === "reset") f.reset(); else f.barrier();
    assert.equal(f.checkpoints[0]?.status, "interrupted", `${boundary} must interrupt the snapshot`);
    assert.equal(f.checkpoints[0]?.epoch, 0);
    assert.equal(f.checkpoints.length, 1);
  }
  const overflow = fixture(); overflow.realtime(); overflow.trace(); overflow.push(pcm(6000));
  overflow.traceCheckpoint(32); overflow.push(pcm(24000));
  assert.equal(overflow.checkpoints[0]?.status, "interrupted", "a dropped prefix cannot be called rendered");
  assert(overflow.stats().droppedSamples > 0);
  assert(overflow.checkpoints[0]!.renderedEnd < overflow.checkpoints[0]!.acceptedEnd);
});
