import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { LocalDeliveryCard, MediaToken } from "./LocalDelivery.js";
import { mediaBudget, MediaBudget, type LocalDelivery } from "./deliveries.js";

const delivery: LocalDelivery = {
  delivery_id: "delivery-one", session_id: "root", channel: "builtin.web", text: "Images", sequence: 1,
  anchor_message_id: "2", call_position: 0, earlier: false,
  assets: [0, 1, 2].map(position => ({
    asset_id: `image-${position}`, filename: `image-${position}.png`, media_type: "image/png", byte_length: 3, position,
  })),
};
const oneImage = { ...delivery, assets: delivery.assets.slice(0, 1) };
const response = () => new Response(new Uint8Array([1, 2, 3]), { headers: { "Content-Type": "image/png" } });

type Observed = { target: Element; margin: string; disconnected: boolean; fire: (visible: boolean) => void };
type Mounted = {
  container: HTMLElement;
  observed: Observed[];
  created: string[];
  revoked: string[];
  render: (value?: LocalDelivery, token?: string) => Promise<void>;
  click: (label: string) => Promise<void>;
};

async function mounted(t: TestContext, run: (view: Mounted) => Promise<void>, observe = false) {
  const dom = new JSDOM("<div id='root'></div>");
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  const observed: Observed[] = [];
  class Observer {
    private current?: Observed;
    constructor(private callback: IntersectionObserverCallback, private options?: IntersectionObserverInit) {}
    observe(target: Element) {
      const current: Observed = {
        target, margin: this.options?.rootMargin || "", disconnected: false,
        fire: visible => this.callback([{ target, isIntersecting: visible } as IntersectionObserverEntry], this as unknown as IntersectionObserver),
      };
      this.current = current; observed.push(current);
    }
    disconnect() { if (this.current) this.current.disconnected = true; }
  }
  Object.assign(globalThis, {
    window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true,
    IntersectionObserver: observe ? Observer : undefined,
  });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const created: string[] = [], revoked: string[] = [];
  t.mock.method(URL, "createObjectURL", () => { const url = `blob:preview-${created.length}`; created.push(url); return url; });
  t.mock.method(URL, "revokeObjectURL", (url: string) => { revoked.push(url); });
  try {
    await run({
      container, observed, created, revoked,
      render: async (value = delivery, token = "token") => {
        await act(async () => root.render(createElement(MediaToken.Provider, { value: token }, createElement(LocalDeliveryCard, { delivery: value }))));
      },
      click: async label => {
        const button = [...container.querySelectorAll("button")].find(element => element.textContent === label);
        assert.ok(button, `Missing ${label} button`);
        await act(async () => button.click());
      },
    });
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT", "IntersectionObserver"]) {
      if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
  assert.deepEqual([...revoked].sort(), [...created].sort(), "Every retained preview is revoked exactly once");
}

function controlledFetch(t: TestContext) {
  const requests: { path: string; token: string; signal: AbortSignal; finish: () => void }[] = [];
  let active = 0, maximum = 0;
  t.mock.method(globalThis, "fetch", (path: string, init: RequestInit) => {
    const signal = init.signal!;
    assert.equal(init.mode, "same-origin");
    assert.equal(init.redirect, "error");
    return new Promise<Response>((resolve, reject) => {
      let settled = false;
      active++; maximum = Math.max(maximum, active);
      const cleanup = () => { settled = true; active--; signal.removeEventListener("abort", abort); };
      const abort = () => { if (!settled) { cleanup(); reject(signal.reason); } };
      const finish = () => { assert.equal(settled, false); cleanup(); resolve(response()); };
      signal.addEventListener("abort", abort, { once: true });
      requests.push({ path, token: new Headers(init.headers).get("X-Ngn-Token") || "", signal, finish });
      if (signal.aborted) abort();
    });
  });
  return { requests, maximum: () => maximum };
}

test("three delivered images automatically appear while at most two authenticated fetches run", async t => {
  const network = controlledFetch(t);
  await mounted(t, async view => {
    await view.render();
    assert.equal(network.requests.length, 2);
    assert.equal(view.container.querySelectorAll("img").length, 0);
    await act(async () => network.requests[0].finish());
    assert.equal(network.requests.length, 3, "Third visible image proceeds as soon as a fetch slot opens");
    await act(async () => { network.requests[1].finish(); network.requests[2].finish(); });
    assert.equal(view.container.querySelectorAll("img").length, 3);
    assert.equal(network.maximum(), 2);
    for (const [index, request] of network.requests.entries()) {
      assert.equal(request.path, `/api/sessions/root/deliveries/delivery-one/assets/image-${index}`);
      assert.equal(request.token, "token");
    }
    await view.render({ ...delivery, assets: delivery.assets.map(asset => ({ ...asset })) });
    assert.equal(network.requests.length, 3, "Snapshot replay preserves already visible previews");
    assert.equal(view.created.length, 3);
    assert.equal(view.container.querySelectorAll("a[download]").length, 3);
  });
});

for (const change of ["token", "root"] as const) {
  test(`${change} changes abort fetching and queued previews before loading the new identity`, async t => {
    const network = controlledFetch(t);
    await mounted(t, async view => {
      await view.render();
      assert.equal(network.requests.length, 2);
      const next = change === "root" ? { ...delivery, session_id: "next-root" } : delivery;
      const token = change === "token" ? "next-token" : "token";
      await view.render(next, token);
      assert.ok(network.requests.slice(0, 2).every(request => request.signal.aborted));
      assert.equal(network.requests.length, 4, "Only two new-identity requests start");
      assert.equal(view.created.length, 0);
      await act(async () => network.requests[2].finish());
      assert.equal(network.requests.length, 5);
      await act(async () => { network.requests[3].finish(); network.requests[4].finish(); });
      assert.equal(view.container.querySelectorAll("img").length, 3);
      assert.ok(network.requests.slice(2).every(request => request.token === token && request.path.includes(`/sessions/${next.session_id}/`)));
      assert.equal(network.maximum(), 2);
      assert.equal(network.requests.slice(0, 2).some(request => request.path.endsWith("image-2")), false);
    });
  });
}

test("images load near the viewport and release memory offscreen without preview controls", async t => {
  const network = controlledFetch(t);
  await mounted(t, async view => {
    await view.render(oneImage);
    assert.equal(network.requests.length, 0);
    assert.equal(view.observed.length, 1);
    assert.equal(view.observed[0].margin, "200px");
    await act(async () => view.observed[0].fire(false));
    assert.equal(network.requests.length, 0);
    await act(async () => view.observed[0].fire(true));
    assert.equal(network.requests.length, 1);
    assert.equal(view.observed[0].disconnected, false);
    await act(async () => view.observed[0].fire(false));
    assert.equal(network.requests[0].signal.aborted, true);
    await act(async () => view.observed[0].fire(true));
    await view.render({ ...oneImage });
    assert.equal(network.requests.length, 2);
    await act(async () => network.requests[1].finish());
    assert.ok(view.container.querySelector("img"));
    assert.doesNotMatch(view.container.textContent!, /preview|Unload|Cancel loading/i);
    await act(async () => view.observed[0].fire(false));
    assert.equal(view.container.querySelector("img"), null);
    assert.equal(view.revoked.length, 1);
    await view.render({ ...oneImage });
    assert.equal(network.requests.length, 2);
    await act(async () => view.observed[0].fire(true));
    await act(async () => network.requests[2].finish());
    assert.ok(view.container.querySelector("img"));
  }, true);
});

test("stale intersection callbacks cannot load another identity and missing authentication stays idle", async t => {
  const network = controlledFetch(t);
  await mounted(t, async view => {
    await view.render(oneImage, "");
    assert.equal(view.observed.length, 0);
    assert.equal(network.requests.length, 0);
    await view.render(oneImage);
    const stale = view.observed[0];
    await view.render({ ...oneImage, session_id: "next-root" });
    assert.equal(stale.disconnected, true);
    await act(async () => stale.fire(true));
    assert.equal(network.requests.length, 0);
    await act(async () => view.observed[1].fire(true));
    assert.equal(network.requests.length, 1);
    assert.match(network.requests[0].path, /\/sessions\/next-root\//);
  }, true);
});

test("images wait quietly for available memory and appear automatically when it is released", async t => {
  const held = Array.from({ length: 3 }, () => {
    const reservation = mediaBudget.reserve(20 * 1024 * 1024); reservation.loaded(); return reservation;
  });
  const fetch = t.mock.method(globalThis, "fetch", async () => response());
  try {
    await mounted(t, async view => {
      await view.render(oneImage);
      assert.doesNotMatch(view.container.textContent!, /budget|preview|Unload/i);
      assert.equal(fetch.mock.callCount(), 0);
      await act(async () => held.forEach(reservation => reservation.release()));
      assert.equal(fetch.mock.callCount(), 1);
      assert.ok(view.container.querySelector("img"));
    });
  } finally { held.forEach(reservation => reservation.release()); }
});

test("network failure remains retryable without looping on replay or intersection callbacks", async t => {
  let calls = 0;
  t.mock.method(globalThis, "fetch", async () => {
    if (++calls === 1) throw new Error("Temporary image failure");
    return response();
  });
  await mounted(t, async view => {
    await view.render(oneImage);
    await act(async () => view.observed[0].fire(true));
    assert.match(view.container.textContent!, /Couldn’t load this image/);
    for (let index = 0; index < 3; index++) {
      await view.render({ ...oneImage });
      await act(async () => view.observed[0].fire(true));
    }
    assert.equal(calls, 1);
    await view.click("Retry");
    assert.equal(calls, 2);
    assert.ok(view.container.querySelector("img"));
  }, true);
});

test("automatic fetch waiting is bounded at sixteen and cancellation frees queue and byte reservations", async () => {
  const budget = new MediaBudget(100, 1);
  const held = budget.reserve(1);
  const controllers = Array.from({ length: 16 }, () => new AbortController());
  const wait = (controller: AbortController) => budget.reserveWhenReady(1, controller.signal)
    .then(reservation => ({ reservation, error: undefined }), (error: unknown) => ({ reservation: undefined, error }));
  const pending = controllers.map(wait);
  await assert.rejects(budget.reserveWhenReady(1, new AbortController().signal), /Too many media/);
  const reason = new Error("Cancelled queued image");
  controllers[0].abort(reason);
  assert.equal((await pending[0]).error, reason);
  const replacementController = new AbortController();
  const replacement = wait(replacementController);
  await assert.rejects(budget.reserveWhenReady(1, new AbortController().signal), /Too many media/);
  held.release();
  const first = await pending[1];
  assert.ok(first.reservation, "A queued image acquires the freed fetch slot");
  for (const controller of controllers) controller.abort();
  replacementController.abort();
  first.reservation.release();
  for (const result of await Promise.all([...pending, replacement])) result.reservation?.release();
  budget.reserve(100).release();
});
