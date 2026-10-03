import assert from "node:assert/strict";
import test from "node:test";
import { JSDOM } from "jsdom";
import { mountSphereRenderer, SPHERE_MAX_PIXELS, type SphereFrame, type SpherePainter, type SphereScene } from "./sphereRenderer.js";

function fixture(reduced = false, supported = true) {
  const dom = new JSDOM("<canvas></canvas>"), view = dom.window, canvas = view.document.querySelector("canvas")!;
  let now = 0, serial = 0, hidden = false, width = 92, height = 92;
  const pending = new Map<number, FrameRequestCallback>(), available: boolean[] = [];
  const paints: { scene: SphereScene; frame: SphereFrame }[] = [], disposals: number[] = [];
  let created = 0, observerDisconnects = 0, sizeDisconnects = 0;
  let intersection!: IntersectionObserverCallback, resized!: ResizeObserverCallback;
  const query = Object.assign(new view.EventTarget(), { matches: reduced });
  const scene: SphereScene = { phase: "connected", muted: false, busy: false, disabled: false, input: 0.2, output: 0.4, x: 0, y: 0, hover: false, pressed: false };
  Object.defineProperty(view.document, "hidden", { get: () => hidden });
  Object.defineProperty(view.performance, "now", { value: () => now });
  Object.defineProperty(view, "devicePixelRatio", { value: 3 });
  Object.defineProperty(view, "matchMedia", { value: () => query });
  Object.defineProperty(view, "requestAnimationFrame", { value: (callback: FrameRequestCallback) => { pending.set(++serial, callback); return serial; } });
  Object.defineProperty(view, "cancelAnimationFrame", { value: (id: number) => pending.delete(id) });
  Object.defineProperty(view, "IntersectionObserver", { value: class {
    constructor(callback: IntersectionObserverCallback) { intersection = callback; }
    observe() {} disconnect() { observerDisconnects++; }
  } });
  Object.defineProperty(view, "ResizeObserver", { value: class {
    constructor(callback: ResizeObserverCallback) { resized = callback; }
    observe() {} disconnect() { sizeDisconnects++; }
  } });
  canvas.getBoundingClientRect = () => ({ width, height, left: 0, top: 0, bottom: height, right: width, x: 0, y: 0, toJSON() { return {}; } });
  const painter = (): SpherePainter | undefined => {
    if (!supported) return undefined;
    const id = ++created;
    return { paint: (value, frame) => paints.push({ scene: { ...value }, frame: { ...frame } }), dispose: () => disposals.push(id) };
  };
  const renderer = mountSphereRenderer(canvas, () => scene, (ready) => available.push(ready), painter);
  return { canvas, scene, paints, disposals, pending, available, renderer,
    created: () => created, disconnected: () => [observerDisconnects, sizeDisconnects],
    tick: (milliseconds = 1000 / 60) => { now += milliseconds; const tasks = [...pending.values()]; pending.clear(); tasks.forEach((callback) => callback(now)); },
    refresh: (milliseconds = 1) => { now += milliseconds; renderer.refresh(); },
    hidden: (value: boolean) => { hidden = value; view.document.dispatchEvent(new view.Event("visibilitychange")); },
    visible: (value: boolean) => intersection([{ target: canvas, isIntersecting: value, intersectionRatio: value ? 1 : 0,
      boundingClientRect: canvas.getBoundingClientRect(), intersectionRect: canvas.getBoundingClientRect(), rootBounds: null, time: now,
    }], {} as IntersectionObserver),
    reduced: (value: boolean) => { query.matches = value; query.dispatchEvent(new view.Event("change")); },
    resize: (value: number) => { width = height = value; resized([], {} as ResizeObserver); },
    lose: () => { const event = new view.Event("webglcontextlost", { cancelable: true }); canvas.dispatchEvent(event); return event.defaultPrevented; },
    restore: () => canvas.dispatchEvent(new view.Event("webglcontextrestored")),
    close: () => { renderer.dispose(); dom.window.close(); },
  };
}

test("sphere rendering stays capped at 30 fps and 256 pixels even on large high-DPI displays", (t) => {
  const f = fixture(); t.after(f.close);
  assert.equal(f.canvas.width, 184); assert.deepEqual(f.available, [true]);
  for (let i = 0; i < 60; i++) f.tick();
  assert.ok(f.paints.length >= 29 && f.paints.length <= 31);
  const count = f.paints.length;
  for (let i = 0; i < 100; i++) f.refresh();
  assert.ok(f.paints.length <= count + 3, "pointer and audio invalidations cannot bypass the frame budget");
  assert.equal(f.pending.size, 1);
  f.resize(900); assert.equal(f.canvas.width, SPHERE_MAX_PIXELS); assert.equal(f.canvas.height, SPHERE_MAX_PIXELS);
  assert.deepEqual(f.available, [true], "React availability state changes only when the renderer changes");
});

test("hidden documents and offscreen spheres pause all frames and resume without replaying hidden animation", (t) => {
  const f = fixture(); t.after(f.close); f.tick(40);
  const count = f.paints.length, time = f.paints.at(-1)!.frame.time;
  f.hidden(true); assert.equal(f.pending.size, 0);
  f.tick(30_000); f.refresh(); assert.equal(f.paints.length, count);
  f.hidden(false); assert.equal(f.pending.size, 1);
  assert.ok(f.paints.at(-1)!.frame.time - time < 0.11);
  f.visible(false); const paused = f.paints.length; f.tick(30_000); f.refresh();
  assert.equal(f.pending.size, 0); assert.equal(f.paints.length, paused);
  f.visible(true); assert.equal(f.pending.size, 1); assert.ok(f.paints.length > paused);
});

test("reduced motion renders a static surface and ignores pointer/audio motion while still showing state changes", (t) => {
  const f = fixture(true); t.after(f.close);
  assert.equal(f.pending.size, 0); assert.equal(f.paints.length, 1); assert.equal(f.paints[0].frame.motion, 0);
  for (let i = 0; i < 100; i++) { f.scene.input = i / 100; f.scene.x = i / 100; f.scene.pressed = true; f.refresh(); }
  assert.equal(f.paints.length, 1);
  f.scene.muted = true; f.refresh(); assert.equal(f.paints.length, 2); assert.equal(f.pending.size, 0);
  f.reduced(false); assert.equal(f.pending.size, 1); f.tick(40);
  assert.ok(f.paints.at(-1)!.frame.motion > 0); assert.ok(f.paints.at(-1)!.frame.x > 0);
  f.reduced(true); assert.equal(f.pending.size, 0); assert.equal(f.paints.at(-1)!.frame.motion, 0);
});

test("context loss shows the fallback, restoration rebuilds once and unmount releases resources and observers", () => {
  const f = fixture();
  assert.equal(f.lose(), true); assert.equal(f.pending.size, 0); assert.deepEqual(f.available, [true, false]);
  f.tick(1000); f.refresh(); assert.equal(f.paints.length, 1);
  f.restore(); assert.equal(f.created(), 2); assert.deepEqual(f.disposals, [1]); assert.deepEqual(f.available, [true, false, true]);
  f.renderer.dispose(); f.renderer.dispose();
  assert.deepEqual(f.disposals, [1, 2]); assert.equal(f.pending.size, 0); assert.deepEqual(f.disconnected(), [1, 1]);
  const count = f.paints.length; f.hidden(false); f.reduced(false); f.restore(); f.renderer.refresh();
  assert.equal(f.paints.length, count); assert.equal(f.created(), 2); f.close();
});

test("unsupported graphics use the fallback without starting animation or retaining observers", (t) => {
  const f = fixture(false, false); t.after(f.close);
  assert.deepEqual(f.available, [false]); assert.equal(f.pending.size, 0); assert.equal(f.paints.length, 0);
  f.renderer.refresh(); f.renderer.dispose(); assert.deepEqual(f.disconnected(), [0, 0]);
});
