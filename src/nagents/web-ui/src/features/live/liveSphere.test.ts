import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { LiveSphere, type LiveSphereProps } from "./LiveSphere.js";
import type { LiveDelegation } from "./types.js";

function view({ supported = true, reduced = false } = {}) {
  const dom = new JSDOM("<div id='root'></div>", { pretendToBeVisual: true });
  const previous = Object.getOwnPropertyDescriptors(globalThis), frames = new Map<number, FrameRequestCallback>();
  let next = 0, now = 0, clicks = 0, draws = 0;
  const observers = new Set<object>();
  class Resize {
    constructor(private callback: ResizeObserverCallback) {}
    observe() { observers.add(this); this.callback([], this as unknown as ResizeObserver); }
    unobserve() {}
    disconnect() { observers.delete(this); }
  }
  const globals = { window: dom.window, document: dom.window.document, ResizeObserver: Resize, IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame(callback: FrameRequestCallback) { const id = ++next; frames.set(id, callback); return id; },
    cancelAnimationFrame(id: number) { frames.delete(id); },
  };
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  Object.defineProperty(dom.window, "CanvasRenderingContext2D", { value: class {} });
  Object.defineProperty(dom.window, "matchMedia", { value: () => ({ matches: reduced, addEventListener() {}, removeEventListener() {} }) });
  const context: Partial<CanvasRenderingContext2D> = {
    clearRect() { draws++; }, fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, arc() {}, stroke() {}, fill() {}, setTransform() {},
    createRadialGradient() { return { addColorStop() {} }; },
  };
  Object.defineProperty(dom.window.HTMLCanvasElement.prototype, "getContext", { configurable: true, value: () => supported ? context : null });
  Object.defineProperty(dom.window.HTMLCanvasElement.prototype, "clientWidth", { configurable: true, get: () => 92 });
  Object.defineProperty(dom.window.HTMLCanvasElement.prototype, "clientHeight", { configurable: true, get: () => 92 });
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  const props: LiveSphereProps = { phase: "connected", sessionId: "voice-1", delegations: [], micMuted: false, outputMuted: false, busy: false, disabled: false, inputLevel: 0, outputLevel: 0, onActivate() { clicks++; } };
  return {
    dom, container, props,
    button: () => container.querySelector<HTMLButtonElement>("button.live-sphere")!,
    canvas: () => container.querySelector<HTMLCanvasElement>("canvas")!,
    counts: () => ({ frames: frames.size, observers: observers.size, clicks, draws }),
    render: async (strict = false) => act(async () => root.render(strict ? createElement(StrictMode, {}, createElement(LiveSphere, props)) : createElement(LiveSphere, props))),
    async step(count = 1) { for (let frame = 0; frame < count; frame++) await act(async () => { now += 17; const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(callback => callback(now)); }); },
    async close() {
      await act(async () => root.unmount()); assert.equal(frames.size, 0); assert.equal(observers.size, 0); dom.window.close();
      for (const key of Object.keys(globals)) { if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key); }
    },
  };
}

const record = (id: string, status: LiveDelegation["status"], seq: number, sessionId = "voice-1"): LiveDelegation => ({ id, status, seq, sessionId, chatSessionId: "chat-1", agent: "researcher", provider: "openai", model: "model", runId: `run-${id}`, text: "" });

test("the approved network remains a single native voice button with a passive canvas", async () => {
  const ui = view();
  try {
    await ui.render(true); await ui.step(12);
    assert.equal(ui.button().dataset.renderer, "network"); assert.equal(ui.canvas().dataset.nodes, "204");
    assert.equal(ui.canvas().width, 92); assert.equal(ui.canvas().tabIndex, -1);
    assert.equal(ui.button().getAttribute("aria-label"), "Mute voice microphone");
    assert.equal(ui.counts().frames, 1); assert.equal(ui.counts().observers, 1);
    await act(async () => ui.canvas().click()); assert.equal(ui.counts().clicks, 1);
    const wheel = new ui.dom.window.WheelEvent("wheel", { bubbles: true, cancelable: true, deltaY: 20 }); ui.canvas().dispatchEvent(wheel); assert.equal(wheel.defaultPrevented, false);
    ui.props.micMuted = true; await ui.render(true); assert.equal(ui.button().getAttribute("aria-label"), "Unmute voice microphone"); assert.equal(ui.button().getAttribute("aria-pressed"), "true");
    ui.props.phase = "connecting"; await ui.render(true); assert.equal(ui.button().disabled, true); assert.equal(ui.button().getAttribute("aria-busy"), "true");
  } finally { await ui.close(); }
});

test("direct audio frames reach the network and obey microphone, output and session gates", async () => {
  const ui = view(); let samples = 0;
  ui.props.audio = { sample() { samples++; return { input: { active: true, rms: .12, low: .02, mid: .01, high: .002 }, output: { active: false, rms: 0, low: 0, mid: 0, high: 0 } }; } };
  try {
    await ui.render(); await ui.step(30);
    assert(samples > 0); assert(Number(ui.canvas().dataset.outerScale) < 1, "microphone input contracts the actual network");
    ui.props.micMuted = true; await ui.render(); await ui.step(40); assert(Math.abs(Number(ui.canvas().dataset.outerScale) - 1) < .0001);
    ui.props.phase = "ended"; await ui.render(); const stopped = samples; await ui.step(12); assert.equal(samples, stopped);
    assert.equal(ui.button().getAttribute("aria-label"), "Start voice again");
  } finally { await ui.close(); }
});

test("polled delegation records use real IDs without restarting or replaying animation", async t => {
  const ui = view(); ui.props.delegations = [record("backend-task", "working", 1)]; ui.props.busy = true;
  try {
    await ui.render(true); await ui.step(130);
    const original = Math.random, random = t.mock.method(Math, "random", () => original());
    ui.button().focus();
    ui.props.delegations = [record("backend-task", "working", 1)]; await ui.render(true);
    ui.props.delegations = [record("backend-task", "working", 2)]; await ui.render(true);
    assert.equal(random.mock.calls.length, 0, "same active ID never starts a new route on polling");
    assert.equal(ui.dom.window.document.activeElement, ui.button());
    ui.props.delegations = [record("backend-task", "completed", 3)]; await ui.render(true);
    assert(random.mock.calls.length > 0, "the matching backend ID delivers its result into the network");
    const delivered = random.mock.calls.length;
    ui.props.delegations = [record("backend-task", "completed", 4), record("historical", "completed", 5)]; await ui.render(true);
    assert.equal(random.mock.calls.length, delivered, "duplicate results and terminal history stay quiet");
    ui.props.sessionId = "voice-2"; ui.props.delegations = [record("backend-task", "working", 1, "voice-2")]; await ui.render(true);
    assert(random.mock.calls.length > delivered, "a new voice session gets fresh visual state");
  } finally { await ui.close(); }
});

test("unsupported canvas and reduced motion retain accessible network controls", async () => {
  const fallback = view({ supported: false });
  try {
    await fallback.render(); assert.equal(fallback.button().dataset.renderer, "fallback");
    assert(fallback.container.querySelector("svg.live-sphere-fallback circle"));
    await act(async () => fallback.button().click()); assert.equal(fallback.counts().clicks, 1);
  } finally { await fallback.close(); }
  const reduced = view({ reduced: true });
  try {
    await reduced.render(); await reduced.step(20); assert.equal(reduced.button().dataset.renderer, "network");
    assert.equal(Number(reduced.canvas().dataset.shock), 0); assert.equal(Number(reduced.canvas().dataset.ambientPackets), 0);
    assert.equal(reduced.button().getAttribute("aria-label"), "Mute voice microphone");
  } finally { await reduced.close(); }
});

test("permission, provider setup and transport setup share a distinct connecting network; failure never listens", async () => {
  const ui = view(); let sampled = 0;
  ui.props.audio = { sample() { sampled++; return { input: { active: true, rms: .2, low: .1, mid: .1, high: .1 }, output: { active: false, rms: 0, low: 0, mid: 0, high: 0 } }; } };
  try {
    for (const phase of ["permission", "connecting"] as const) {
      ui.props.phase = phase; ui.props.busy = true;
      await ui.render(); await ui.step(30);
      assert.equal(ui.canvas().dataset.mode, "connect");
      assert.equal(ui.canvas().dataset.ambientPackets, "0", "setup never runs the thinking/listening packets");
      assert.equal(ui.canvas().dataset.voicePackets, "0");
      assert.equal(ui.button().getAttribute("aria-busy"), "true");
      assert.equal(ui.canvas().dataset.nodes, "204", "connection does not replace the approved network");
    }
    assert.equal(sampled, 0, "setup cannot consume voice frames");
    ui.props.phase = "error"; await ui.render(); await ui.step(30);
    assert.equal(ui.canvas().dataset.mode, "error");
    assert.equal(ui.canvas().dataset.ambientPackets, "0");
    assert.equal(ui.button().getAttribute("aria-busy"), null);
    assert.equal(ui.button().getAttribute("aria-label"), "Reconnect voice");
    assert.equal(ui.button().disabled, false);
    assert.equal(sampled, 0);
    ui.props.phase = "connecting"; await ui.render(); await ui.step(12);
    assert.equal(ui.canvas().dataset.mode, "connect", "retry visibly re-enters setup");
    ui.props.phase = "connected"; ui.props.busy = false; await ui.render(); await ui.step(30);
    assert.equal(ui.canvas().dataset.mode, "listen");
    assert(Number(ui.canvas().dataset.outerScale) < 1);
    assert(sampled > 0, "only the ready connection receives microphone frames");
  } finally { await ui.close(); }
});

test("connection and failure indicators survive reduced motion and a missing canvas", async () => {
  for (const options of [{ supported: false }, { reduced: true }]) {
    const ui = view(options);
    try {
      ui.props.phase = "connecting"; await ui.render(); await ui.step(12);
      assert(ui.container.querySelector("svg.live-sphere-connection .live-sphere-connection-arcs"));
      assert.equal(ui.button().getAttribute("aria-label"), "Connecting voice");
      ui.props.phase = "error"; await ui.render(); await ui.step(12);
      assert.equal(ui.button().getAttribute("aria-label"), "Reconnect voice");
      if (options.reduced) {
        assert.equal(ui.canvas().dataset.mode, "error");
        assert.equal(ui.canvas().dataset.shock, "0");
      }
    } finally { await ui.close(); }
  }
});
