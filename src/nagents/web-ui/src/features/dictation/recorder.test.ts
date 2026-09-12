import assert from "node:assert/strict";
import test from "node:test";
import { createRecording, type AudioEnvironment } from "./recorder.js";
import { deferred, dictationConfig } from "./testFixtures.js";
import type { RecordedAudio } from "./types.js";

function fixture(rate = 48000) {
  const calls: string[] = [];
  const results: RecordedAudio[] = [];
  const errors: unknown[] = [];
  class Track extends EventTarget { stop() { calls.push("track.stop"); } }
  const track = new Track();
  const stream = { getTracks: () => [track] } as unknown as MediaStream;
  const source = { connect: () => calls.push("source.connect"), disconnect: () => calls.push("source.disconnect") };
  const silence = { gain: { value: 1 }, connect: () => calls.push("silence.connect"), disconnect: () => calls.push("silence.disconnect") };
  const port = {
    onmessage: null as ((event: MessageEvent<unknown>) => void) | null,
    onmessageerror: null as (() => void) | null,
    postMessage: () => { calls.push("stop.request"); },
    close: () => { calls.push("port.close"); },
  };
  const node = { port, onprocessorerror: null as (() => void) | null, connect: () => calls.push("node.connect"), disconnect: () => calls.push("node.disconnect") };
  const context = {
    sampleRate: rate, state: "running", onstatechange: null as (() => void) | null,
    resume: async () => { calls.push("context.resume"); },
    close: async () => { calls.push("context.close"); },
    audioWorklet: { addModule: async (url: string) => { assert.equal(url, "/assets/synthetic-worklet.js"); calls.push("module.load"); } },
    createMediaStreamSource: () => source,
    createGain: () => silence,
    destination: {},
  };
  const environment: AudioEnvironment = {
    getStream: async () => { calls.push("permission"); return stream; },
    createContext: () => { calls.push("context.create"); return context as unknown as AudioContext; },
    createNode: (_context, maxFrames) => { assert.equal(maxFrames, rate); calls.push("node.create"); return node as unknown as AudioWorkletNode; },
    workletUrl: "/assets/synthetic-worklet.js",
  };
  const recording = createRecording(dictationConfig, { stopped: (audio) => results.push(audio), failed: (cause) => errors.push(cause) }, environment);
  const send = (data: unknown) => port.onmessage?.({ data } as MessageEvent<unknown>);
  const count = (value: string) => calls.filter((call) => call === value).length;
  return { calls, results, errors, track, stream, context, environment, recording, port, node, silence, send, count };
}

test("capture waits for final samples, resamples the actual rate, is silent, and releases every resource once", async () => {
  const f = fixture();
  assert.deepEqual(f.calls, []);
  await f.recording.start();
  await f.recording.start();
  assert.equal(f.count("permission"), 1);
  assert.equal(f.silence.gain.value, 0);
  f.send({ type: "samples", samples: new Float32Array(480).fill(0.5) });
  f.recording.stop();
  f.recording.stop();
  assert.equal(f.count("stop.request"), 1);
  assert.equal(f.results.length, 0);
  f.send({ type: "samples", samples: new Float32Array(480).fill(0.25) });
  f.send({ type: "stopped", limited: false });
  assert.equal(f.results.length, 1);
  assert.equal(f.results[0].blob.size, 44 + 320 * 2);
  assert.equal(f.results[0].seconds, 0.02);
  assert.equal(f.results[0].limited, false);
  f.recording.cancel();
  for (const call of ["port.close", "source.disconnect", "node.disconnect", "silence.disconnect", "track.stop", "context.close"])
    assert.equal(f.count(call), 1, call);
  assert.equal(f.port.onmessage, null);
  assert.equal(f.node.onprocessorerror, null);
  assert.equal(f.context.onstatechange, null);
  assert.deepEqual(f.errors, []);
});

test("cancelled permission prompts release any late stream without loading or connecting a worklet", async () => {
  const f = fixture();
  const permission = deferred<MediaStream>();
  f.environment.getStream = () => permission.promise;
  const pending = f.recording.start();
  f.recording.cancel();
  f.recording.cancel();
  permission.resolve(f.stream);
  await pending;
  assert.equal(f.count("track.stop"), 1);
  assert.equal(f.count("context.close"), 1);
  assert.equal(f.count("module.load"), 0);
  assert.equal(f.count("node.create"), 0);
  assert.deepEqual(f.results, []);
});

test("cancellation while the worklet loads ignores completion and releases the stream/context", async () => {
  const f = fixture();
  const module = deferred<void>();
  const loading = deferred<void>();
  f.context.audioWorklet.addModule = () => { loading.resolve(); return module.promise; };
  const pending = f.recording.start();
  await loading.promise;
  f.recording.cancel();
  module.resolve();
  await pending;
  assert.equal(f.count("track.stop"), 1);
  assert.equal(f.count("context.close"), 1);
  assert.equal(f.count("node.create"), 0);
});

for (const stage of ["permission", "module"]) {
  for (const notify of [true, false]) {
    test(`suspension during held ${stage} ${notify ? "is observed immediately" : "is caught before readiness without an event"}`, async () => {
      const f = fixture();
      const waiting = deferred<void>();
      const permission = deferred<MediaStream>();
      const module = deferred<void>();
      if (stage === "permission") {
        f.environment.getStream = () => { waiting.resolve(); return permission.promise; };
      } else {
        f.context.audioWorklet.addModule = () => { waiting.resolve(); return module.promise; };
      }
      const start = f.recording.start();
      await waiting.promise;
      const stateChanged = f.context.onstatechange;
      assert.ok(stateChanged, "State observation must already be installed during startup");
      f.context.state = "suspended";
      if (notify) {
        stateChanged();
        assert.equal(f.errors.length, 1);
        assert.equal(f.count("context.close"), 1);
        assert.equal(f.count("track.stop"), stage === "module" ? 1 : 0);
      }
      permission.resolve(f.stream);
      module.resolve();
      await start;
      assert.equal(f.errors.length, 1);
      assert.match(String(f.errors[0]), /interrupted.*record again/);
      assert.equal(f.count("track.stop"), 1);
      assert.equal(f.count("context.close"), 1);
      assert.equal(f.count("node.create"), 0);
      assert.equal(f.count("source.connect"), 0);
      assert.equal(f.count("context.resume"), 1);
      assert.equal(f.context.onstatechange, null);
      assert.equal(f.results.length, 0);
      // A late running event may not revive the interrupted operation or resume
      // it without a fresh user action.
      f.context.state = "running";
      stateChanged();
      f.recording.cancel();
      assert.equal(f.count("context.resume"), 1);
      assert.equal(f.count("track.stop"), 1);
    });
  }
}

test("initial suspended state can reach running through its single user-initiated resume", async () => {
  const f = fixture();
  const resumed = deferred<void>();
  f.context.state = "suspended";
  f.context.resume = () => { f.calls.push("context.resume"); return resumed.promise; };
  const start = f.recording.start();
  f.context.onstatechange?.();
  assert.equal(f.errors.length, 0);
  assert.equal(f.count("node.create"), 0);
  f.context.state = "running";
  f.context.onstatechange?.();
  resumed.resolve();
  await start;
  assert.equal(f.count("context.resume"), 1);
  assert.equal(f.count("node.create"), 1);
  assert.equal(f.errors.length, 0);
  f.recording.cancel();
  assert.equal(f.count("track.stop"), 1);
});

test("a resume promise that resolves while still suspended does not report capture ready", async () => {
  const f = fixture();
  f.context.state = "suspended";
  await f.recording.start();
  assert.equal(f.errors.length, 1);
  assert.equal(f.count("node.create"), 0);
  assert.equal(f.count("track.stop"), 1);
  assert.equal(f.count("context.close"), 1);
  assert.equal(f.count("context.resume"), 1);
});

test("unsupported requested sample rate falls back to the actual context rate", async () => {
  const f = fixture(44100);
  const requested: Array<number | undefined> = [];
  f.environment.createContext = (rate) => {
    requested.push(rate);
    if (rate) throw new DOMException("Rate unavailable", "NotSupportedError");
    return f.context as unknown as AudioContext;
  };
  await f.recording.start();
  assert.deepEqual(requested, [16000, undefined]);
  f.send({ type: "samples", samples: new Float32Array(441) });
  f.recording.stop();
  f.send({ type: "stopped", limited: false });
  assert.equal(f.results[0].blob.size, 364);
});

test("cancelling an active or stopping recorder releases nodes and ignores already queued final messages", async () => {
  for (const stopping of [false, true]) {
    const f = fixture();
    await f.recording.start();
    f.send({ type: "samples", samples: new Float32Array(480) });
    const late = f.port.onmessage;
    if (stopping) f.recording.stop();
    f.recording.cancel();
    f.recording.cancel();
    late?.({ data: { type: "stopped", limited: false } } as MessageEvent<unknown>);
    for (const call of ["track.stop", "context.close", "port.close", "node.disconnect", "source.disconnect"])
      assert.equal(f.count(call), 1, call);
    assert.equal(f.results.length, 0);
    assert.equal(f.errors.length, 0);
  }
});

test("a failed context resume releases a later permission grant without creating audio nodes", async () => {
  const f = fixture();
  const permission = deferred<MediaStream>();
  f.environment.getStream = () => permission.promise;
  f.context.resume = async () => { throw new Error("Audio could not resume"); };
  const pending = f.recording.start();
  await Promise.resolve();
  permission.resolve(f.stream);
  await pending;
  assert.equal(f.count("track.stop"), 1);
  assert.equal(f.count("context.close"), 1);
  assert.equal(f.count("node.create"), 0);
  assert.equal(f.errors.length, 1);
});

test("duration timer stops locally and marks the result as requiring explicit transcription", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fixture();
  await f.recording.start();
  f.send({ type: "samples", samples: new Float32Array(4800) });
  t.mock.timers.tick(1000);
  assert.equal(f.count("stop.request"), 1);
  assert.equal(f.results.length, 0);
  f.send({ type: "stopped", limited: false });
  assert.equal(f.results[0].limited, true);
  assert.equal(f.count("track.stop"), 1);
});

test("missing final acknowledgement fails boundedly and never uploads partial audio", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const f = fixture();
  await f.recording.start();
  f.send({ type: "samples", samples: new Float32Array(480) });
  f.recording.stop();
  t.mock.timers.tick(2000);
  assert.equal(f.errors.length, 1);
  assert.equal(f.count("port.close"), 1);
  assert.deepEqual(f.results, []);
});

for (const failure of ["permission", "module", "processor", "device", "suspended", "empty", "overflow", "protocol"]) {
  test(`${failure} failure keeps audio out of callbacks and releases resources`, async () => {
    const f = fixture();
    if (failure === "permission") f.environment.getStream = async () => { throw new DOMException("Denied", "NotAllowedError"); };
    if (failure === "module") f.context.audioWorklet.addModule = async () => { throw new Error("Unavailable"); };
    await f.recording.start();
    if (failure === "processor") f.node.onprocessorerror?.();
    if (failure === "device") f.track.dispatchEvent(new Event("ended"));
    if (failure === "suspended") { f.context.state = "suspended"; f.context.onstatechange?.(); }
    if (failure === "empty") f.send({ type: "stopped", limited: false });
    if (failure === "overflow") f.send({ type: "samples", samples: new Float32Array(48001) });
    if (failure === "protocol") f.send({ type: "samples", samples: "invalid" });
    f.recording.cancel();
    assert.equal(f.errors.length, 1);
    assert.equal(f.count("context.close"), 1);
    assert.equal(f.count("track.stop"), failure === "permission" ? 0 : 1);
    assert.deepEqual(f.results, []);
  });
}
