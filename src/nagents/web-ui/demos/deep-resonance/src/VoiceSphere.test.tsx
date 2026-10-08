import assert from "node:assert/strict";
import { test } from "node:test";
import { JSDOM } from "jsdom";
import { act, createRef, StrictMode, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { VoiceSphere, VoiceSphereCanvas } from "./VoiceSphere";
import { useVoiceSphere, type AudioSource, type VoiceSphereController, type VoiceSphereHandle } from "./useVoiceSphere";
import { silentSignal } from "./types";

function browser(options: { intersection?: boolean; canvas?: boolean } = {}) {
  const dom = new JSDOM("<!doctype html><html><body></body></html>", { pretendToBeVisual: true });
  const saved = new Map<string, PropertyDescriptor | undefined>();
  const install = (key: string, value: unknown): void => {
    saved.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  };
  const frames = new Map<number, FrameRequestCallback>();
  let nextFrame = 1, time = 0, draws = 0;
  const observers = new Set<TestResizeObserver>();
  const intersections = new Set<TestIntersectionObserver>();
  const canvasDraws = new WeakMap<HTMLCanvasElement, number>();
  class TestResizeObserver implements ResizeObserver {
    readonly targets = new Set<Element>();
    constructor(private readonly callback: ResizeObserverCallback) { observers.add(this); }
    observe(target: Element): void { this.targets.add(target); this.callback([], this); }
    unobserve(target: Element): void { this.targets.delete(target); }
    disconnect(): void { this.targets.clear(); observers.delete(this); }
  }
  class TestIntersectionObserver implements IntersectionObserver {
    readonly targets = new Set<Element>();
    readonly root = null;
    readonly rootMargin = "0px";
    readonly thresholds = [0];
    constructor(private readonly callback: IntersectionObserverCallback) { intersections.add(this); }
    observe(target: Element): void { this.targets.add(target); }
    unobserve(target: Element): void { this.targets.delete(target); }
    disconnect(): void { this.targets.clear(); intersections.delete(this); }
    takeRecords(): IntersectionObserverEntry[] { return []; }
    reveal(target: Element, visible: boolean): void {
      if (!this.targets.has(target)) return;
      const rectangle = target.getBoundingClientRect();
      this.callback([{ target, time, isIntersecting: visible, intersectionRatio: visible ? 1 : 0,
        rootBounds: null, boundingClientRect: rectangle, intersectionRect: rectangle }], this);
    }
  }
  const request = (callback: FrameRequestCallback): number => { const id = nextFrame++; frames.set(id, callback); return id; };
  const cancel = (id: number): void => { frames.delete(id); };
  install("window", dom.window); install("document", dom.window.document); install("navigator", dom.window.navigator);
  install("HTMLElement", dom.window.HTMLElement); install("HTMLCanvasElement", dom.window.HTMLCanvasElement);
  install("ResizeObserver", TestResizeObserver); install("requestAnimationFrame", request); install("cancelAnimationFrame", cancel);
  if (options.intersection) install("IntersectionObserver", TestIntersectionObserver);
  install("IS_REACT_ACT_ENVIRONMENT", true);
  dom.window.requestAnimationFrame = request; dom.window.cancelAnimationFrame = cancel;

  const visibility = new Set<EventListenerOrEventListenerObject>();
  const add = dom.window.document.addEventListener.bind(dom.window.document), remove = dom.window.document.removeEventListener.bind(dom.window.document);
  Object.defineProperty(dom.window.document, "addEventListener", { configurable: true, value(type: string, listener: EventListenerOrEventListenerObject, options?: boolean | AddEventListenerOptions) { if (type === "visibilitychange") visibility.add(listener); add(type, listener, options); } });
  Object.defineProperty(dom.window.document, "removeEventListener", { configurable: true, value(type: string, listener: EventListenerOrEventListenerObject, options?: boolean | EventListenerOptions) { if (type === "visibilitychange") visibility.delete(listener); remove(type, listener, options); } });

  const contexts = new WeakMap<HTMLCanvasElement, CanvasRenderingContext2D>();
  Object.defineProperty(dom.window.HTMLCanvasElement.prototype, "clientWidth", { configurable: true, get(this: HTMLCanvasElement) { return Number.parseFloat(this.style.width) || 320; } });
  Object.defineProperty(dom.window.HTMLCanvasElement.prototype, "clientHeight", { configurable: true, get(this: HTMLCanvasElement) { return Number.parseFloat(this.style.height) || 320; } });
  Object.defineProperty(dom.window.HTMLCanvasElement.prototype, "getContext", { configurable: true, value(this: HTMLCanvasElement, kind: string) {
    if (kind !== "2d" || options.canvas === false) return null;
    const canvas = this;
    const existing = contexts.get(this); if (existing) return existing;
    const finite = (...values: number[]): void => { assert(values.every(Number.isFinite)); };
    const context: Partial<CanvasRenderingContext2D> = {
      fillStyle: "#000", strokeStyle: "#000", lineWidth: 1,
      setTransform(a?: number | DOMMatrix2DInit, b?: number, c?: number, d?: number, e?: number, f?: number) { if (typeof a === "number") finite(a, b ?? 0, c ?? 0, d ?? 0, e ?? 0, f ?? 0); }, clearRect(...values) { finite(...values); draws++; canvasDraws.set(canvas, (canvasDraws.get(canvas) || 0) + 1); }, fillRect: finite, moveTo: finite, lineTo: finite,
      beginPath() {}, fill() {}, stroke() {}, arc(x, y, radius, start, end) { finite(x, y, radius, start, end); },
      createRadialGradient(...values) { finite(...values); return { addColorStop(offset, color) { assert(Number.isFinite(offset)); assert(!color.includes("NaN")); } }; },
    };
    const result = context as CanvasRenderingContext2D; contexts.set(this, result); return result;
  } });
  const container = dom.window.document.createElement("div"); dom.window.document.body.append(container);
  const root = createRoot(container);
  return {
    root, container,
    pendingFrames: () => frames.size,
    activeObservers: () => observers.size,
    activeIntersections: () => intersections.size,
    activeVisibility: () => visibility.size,
    draws: (canvas?: HTMLCanvasElement) => canvas ? canvasDraws.get(canvas) || 0 : draws,
    async intersect(target: Element, visible: boolean) { await act(async () => { intersections.forEach(observer => observer.reveal(target, visible)); }); },
    async hidden(value: boolean) { await act(async () => {
      Object.defineProperty(dom.window.document, "hidden", { configurable: true, value });
      dom.window.document.dispatchEvent(new dom.window.Event("visibilitychange"));
    }); },
    async step(count = 1) {
      for (let index = 0; index < count; index++) await act(async () => {
        time += 17; const callbacks = [...frames.values()]; frames.clear(); callbacks.forEach(callback => callback(time));
      });
    },
    async close() {
      await act(async () => root.unmount());
      assert.equal(frames.size, 0, "unmount must cancel all frame callbacks");
      assert.equal(observers.size, 0, "unmount must disconnect every canvas observer");
      assert.equal(intersections.size, 0, "unmount must disconnect every intersection observer");
      assert.equal(visibility.size, 0, "unmount must remove visibility listeners");
      dom.window.close();
      for (const [key, descriptor] of saved) { if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key); }
    },
  };
}

async function render(root: Root, content: ReactNode): Promise<void> { await act(async () => root.render(content)); }

test("StrictMode owns one animation loop and cleans every lifecycle resource", async () => {
  const env = browser(), ref = createRef<VoiceSphereHandle>();
  try {
    await render(env.root, <StrictMode><VoiceSphere ref={ref} /></StrictMode>);
    assert.equal(env.pendingFrames(), 1); assert.equal(env.activeObservers(), 1); assert.equal(env.activeVisibility(), 1);
    assert.equal(ref.current?.getSnapshot().nodes, 204);
    await env.step(12); assert.equal(env.pendingFrames(), 1); assert(env.draws() >= 12);
    assert.equal(ref.current?.getSnapshot().ambientPackets, 3);
  } finally { await env.close(); }
});

test("two instances stay isolated and removing one preserves the other instance", async () => {
  const env = browser(), first = createRef<VoiceSphereHandle>(), second = createRef<VoiceSphereHandle>();
  const tree = (includeFirst: boolean) => <>{includeFirst && <VoiceSphere key="first" ref={first} />}<VoiceSphere key="second" ref={second} compact /></>;
  try {
    await render(env.root, tree(true)); assert.equal(env.pendingFrames(), 2); assert.equal(env.activeObservers(), 2);
    await act(async () => { assert(first.current?.dispatch({ type: "delegation-start", id: "first-task" })); first.current?.zoomBy(.2); assert(second.current?.dispatch({ type: "delegation-start", id: "second-task" })); });
    assert.deepEqual(first.current?.getSnapshot().tasks.map(task => task.id), ["first-task"]);
    assert.deepEqual(second.current?.getSnapshot().tasks.map(task => task.id), ["second-task"]); assert.equal(second.current?.getSnapshot().zoom, 1);
    const before = second.current;
    await render(env.root, tree(false)); assert.equal(first.current, null); assert.equal(second.current, before);
    assert.equal(env.pendingFrames(), 1); assert.equal(env.activeObservers(), 1); assert.equal(env.activeVisibility(), 1);
    await env.step(130); assert.equal(second.current?.getSnapshot().tasks[0].phase, "pending");
  } finally { await env.close(); }
});

test("changing an audio adapter or snapshot callback does not reset the engine", async () => {
  const env = browser(), ref = createRef<VoiceSphereHandle>(); let firstSamples = 0, secondSamples = 0, snapshots = 0;
  const first: AudioSource = { sample() { firstSamples++; return { input: silentSignal(), output: silentSignal() }; } };
  const second: AudioSource = { sample() { secondSamples++; return { input: { active: true, rms: .12, low: .04, mid: .02, high: .002 }, output: silentSignal() }; } };
  try {
    await render(env.root, <VoiceSphere ref={ref} audio={first} />); await env.step(4);
    await act(async () => { assert(ref.current?.dispatch({ type: "delegation-start", id: "keep-me", label: "Existing task" })); ref.current?.zoomBy(.15); });
    const calls = firstSamples, canvas = env.container.querySelector("canvas");
    await render(env.root, <VoiceSphere ref={ref} audio={second} onSnapshot={() => { snapshots++; }} />); await env.step(15);
    assert.equal(firstSamples, calls); assert(secondSamples > 0); assert(snapshots > 0);
    assert.equal(env.container.querySelector("canvas"), canvas); assert.equal(env.pendingFrames(), 1); assert.equal(env.activeObservers(), 1);
    const snapshot = ref.current?.getSnapshot(); assert.equal(snapshot?.tasks[0].id, "keep-me"); assert.equal(snapshot?.tasks[0].label, "Existing task"); assert.equal(snapshot?.zoom, 1.15); assert((snapshot?.inputLevel ?? 0) > 0);
  } finally { await env.close(); }
});

test("the public event API queues fast results and retains task identity through density changes", async () => {
  const env = browser(), ref = createRef<VoiceSphereHandle>();
  try {
    await render(env.root, <VoiceSphere ref={ref} density={3} />);
    await act(async () => {
      assert(ref.current?.dispatch({ type: "delegation-start", id: "host-7", label: "Review" }));
      assert(ref.current?.dispatch({ type: "delegation-result", id: "host-7" }));
      assert.equal(ref.current?.dispatch({ type: "delegation-result", id: "host-7" }), false);
      assert.equal(ref.current?.dispatch({ type: "delegation-start", id: "host-7" }), false);
    });
    assert.equal(ref.current?.getSnapshot().tasks[0].resultQueued, true);
    await render(env.root, <VoiceSphere ref={ref} density={4} />); assert.equal(ref.current?.getSnapshot().nodes, 444);
    assert.equal(ref.current?.getSnapshot().tasks[0].id, "host-7"); assert.equal(ref.current?.getSnapshot().tasks[0].resultQueued, true);
    await env.step(190); assert.equal(ref.current?.getSnapshot().tasks[0].phase, "complete");
    await act(async () => {
      assert.equal(ref.current?.dispatch({ type: "delegation-result", id: "missing" }), false);
      assert(ref.current?.dispatch({ type: "delegations-reset" }));
      assert(ref.current?.dispatch({ type: "delegation-start", id: "host-7" }));
    });
  } finally { await env.close(); }
});

test("offscreen surfaces stop audio sampling, simulation and publications until visible again", async () => {
  const env = browser({ intersection: true }), ref = createRef<VoiceSphereHandle>();
  let samples = 0, publications = 0;
  const audio: AudioSource = { sample() { samples++; return { input: silentSignal(), output: silentSignal() }; } };
  try {
    await render(env.root, <StrictMode><VoiceSphere ref={ref} audio={audio} onSnapshot={() => { publications++; }} /></StrictMode>);
    const canvas = env.container.querySelector("canvas")!;
    assert.equal(env.activeIntersections(), 1); assert.equal(env.pendingFrames(), 0);
    await env.step(20); assert.equal(samples, 0); assert.equal(publications, 0); assert.equal(env.draws(), 0);
    await env.intersect(canvas, true); assert.equal(env.pendingFrames(), 1); await env.step(8);
    assert(samples > 0); assert(publications > 0); assert(env.draws() > 0);
    await act(async () => { assert(ref.current?.dispatch({ type: "delegation-start", id: "visible-later" })); });
    await env.step(4); await env.intersect(canvas, false); assert.equal(env.pendingFrames(), 0);
    const before = { samples, publications, draws: env.draws() };
    await env.step(300); assert.deepEqual({ samples, publications, draws: env.draws() }, before);
    assert.equal(ref.current?.getSnapshot().tasks[0].phase, "launch");
    await env.intersect(canvas, true); await env.step(1);
    assert.equal(ref.current?.getSnapshot().tasks[0].phase, "launch", "offscreen time must not advance an animation on resume");
    assert.equal(env.pendingFrames(), 1);
    await env.hidden(true); assert.equal(env.pendingFrames(), 0);
    const hiddenSamples = samples; await env.step(100); assert.equal(samples, hiddenSamples);
    await env.hidden(false); await env.step(1); assert.equal(samples, hiddenSamples + 1);
  } finally { await env.close(); }
});

test("one visible canvas keeps a shared controller alive and removing its last surface suspends it", async () => {
  const env = browser({ intersection: true }); let samples = 0, controller: VoiceSphereController | undefined;
  const audio: AudioSource = { sample() { samples++; return { input: silentSignal(), output: silentSignal() }; } };
  function Views({ count }: { count: number }) {
    controller = useVoiceSphere({ audio });
    return <>{count > 0 && <VoiceSphereCanvas key="large" controller={controller} />}{count > 1 && <VoiceSphereCanvas key="small" controller={controller} compact />}</>;
  }
  try {
    await render(env.root, <Views count={2} />);
    const [main, mini] = [...env.container.querySelectorAll("canvas")];
    await env.intersect(mini, true); await env.step(5);
    assert.equal(env.draws(main), 0); assert(env.draws(mini) > 0); assert.equal(env.pendingFrames(), 1);
    await env.intersect(main, true); await env.intersect(mini, false); await env.step(5); assert(env.draws(main) > 0);
    await act(async () => { assert(controller?.dispatch({ type: "delegation-start", id: "preserved" })); });
    await render(env.root, <Views count={0} />); assert.equal(env.pendingFrames(), 0); assert.equal(env.activeIntersections(), 0);
    const before = samples; await env.step(100); assert.equal(samples, before);
    await render(env.root, <Views count={1} />); assert.equal(controller?.getSnapshot().tasks[0].id, "preserved");
    await env.intersect(env.container.querySelector("canvas")!, true); await env.step(2); assert(samples > before);
  } finally { await env.close(); }
});

test("unavailable canvas owns no frame work while reduced-motion explicit events still settle", async () => {
  const env = browser({ canvas: false, intersection: true }), ref = createRef<VoiceSphereHandle>(); let samples = 0;
  try {
    await render(env.root, <StrictMode><VoiceSphere ref={ref} reducedMotion audio={{ sample() { samples++; return { input: silentSignal(), output: silentSignal() }; } }} /></StrictMode>);
    assert.equal(env.pendingFrames(), 0); assert.equal(env.activeObservers(), 0); assert.equal(env.activeIntersections(), 0);
    await env.step(100); assert.equal(samples, 0); assert.equal(env.draws(), 0);
    await act(async () => {
      assert(ref.current?.dispatch({ type: "delegation-start", id: "no-canvas" }));
      assert(ref.current?.dispatch({ type: "delegation-result", id: "no-canvas" }));
    });
    assert.equal(ref.current?.getSnapshot().tasks[0].phase, "complete"); assert.equal(env.pendingFrames(), 0);
  } finally { await env.close(); }
});
