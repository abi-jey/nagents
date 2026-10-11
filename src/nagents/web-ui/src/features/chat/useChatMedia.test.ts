import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { loadMedia, mediaBudget, MediaBudget } from "./deliveries.js";
import { useChatMedia } from "./useChatMedia.js";

type Options = Parameters<typeof useChatMedia>[0];
const initial: Options = {
  token: "private-token", path: "sessions/root/uploads/image/preview", asset: { media_type: "image/png", byte_length: 3 },
};
const response = () => new Response(new Uint8Array([1, 2, 3]), { headers: { "Content-Type": "image/png" } });
type Observed = { margin: string; disconnected: boolean; fire: (near: boolean) => void };
type View = {
  container: HTMLElement;
  observed: Observed[];
  created: string[];
  revoked: string[];
  render: (changes?: Partial<Options>) => Promise<void>;
  unmount: () => Promise<void>;
  retry: () => Promise<void>;
  fire: (near: boolean, index?: number) => Promise<void>;
  status: () => string;
  url: () => string;
};

async function mounted(t: TestContext, run: (view: View) => Promise<void>, observe = true) {
  const dom = new JSDOM("<div id='root'></div>");
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  const observed: Observed[] = [];
  class Observer {
    private entry?: Observed;
    constructor(private callback: IntersectionObserverCallback, private options?: IntersectionObserverInit) {}
    observe(target: Element) {
      const entry: Observed = {
        margin: this.options?.rootMargin || "", disconnected: false,
        fire: near => this.callback([{ target, isIntersecting: near } as IntersectionObserverEntry], this as unknown as IntersectionObserver),
      };
      this.entry = entry;
      observed.push(entry);
    }
    disconnect() { if (this.entry) this.entry.disconnected = true; }
  }
  Object.assign(globalThis, {
    window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true,
    IntersectionObserver: observe ? Observer : undefined,
  });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const created: string[] = [], revoked: string[] = [];
  t.mock.method(URL, "createObjectURL", () => { const url = `blob:media-${created.length}`; created.push(url); return url; });
  t.mock.method(URL, "revokeObjectURL", (url: string) => { revoked.push(url); });
  let options = initial, unmounted = false;
  function Media(props: Options) {
    const media = useChatMedia(props);
    return createElement("figure", { ref: media.container, "data-status": media.status },
      media.url ? createElement("img", { src: media.url }) : media.error,
      createElement("button", { onClick: media.retry }, "Retry"));
  }
  const unmount = async () => {
    if (!unmounted) { unmounted = true; await act(async () => root.unmount()); }
  };
  try {
    await run({
      container, observed, created, revoked, unmount,
      render: async changes => {
        options = { ...options, ...changes };
        await act(async () => root.render(createElement(Media, options)));
      },
      retry: async () => { await act(async () => container.querySelector<HTMLButtonElement>("button")!.click()); },
      fire: async (near, index = observed.length - 1) => { await act(async () => observed[index].fire(near)); },
      status: () => container.querySelector("figure")?.getAttribute("data-status") || "",
      url: () => container.querySelector("img")?.getAttribute("src") || "",
    });
  } finally {
    await unmount();
    dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT", "IntersectionObserver"]) {
      if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
  assert.deepEqual([...revoked].sort(), [...created].sort(), "Every created URL is released exactly once");
}

function controlledFetch(t: TestContext, honorAbort = true) {
  const requests: { path: string; token: string; signal: AbortSignal; finish: () => void }[] = [];
  t.mock.method(globalThis, "fetch", (path: string, init: RequestInit) => {
    const signal = init.signal!;
    assert.equal(init.mode, "same-origin");
    assert.equal(init.redirect, "error");
    assert.doesNotMatch(path, /private-token/);
    return new Promise<Response>((resolve, reject) => {
      let settled = false;
      const cleanup = () => { settled = true; signal.removeEventListener("abort", abort); };
      const abort = () => { if (honorAbort && !settled) { cleanup(); reject(signal.reason); } };
      signal.addEventListener("abort", abort, { once: true });
      requests.push({
        path, token: new Headers(init.headers).get("X-Ngn-Token") || "", signal,
        finish: () => { assert.equal(settled, false); cleanup(); resolve(response()); },
      });
      if (signal.aborted) abort();
    });
  });
  return requests;
}

test("media appears near the viewport, releases offscreen and reloads once on reentry", async t => {
  const requests = controlledFetch(t);
  await mounted(t, async view => {
    await view.render();
    assert.equal(view.observed[0].margin, "200px");
    assert.equal(view.status(), "waiting");
    assert.equal(requests.length, 0);
    await view.fire(true);
    assert.equal(view.status(), "loading");
    assert.equal(requests.length, 1);
    assert.equal(requests[0].token, "private-token");
    await act(async () => requests[0].finish());
    assert.equal(view.status(), "ready");
    assert.equal(view.url(), "blob:media-0");
    await view.fire(true);
    await view.render({ asset: { ...initial.asset } });
    assert.equal(requests.length, 1);
    assert.equal(view.observed[0].disconnected, false, "Keep observing after the initial load");
    await view.fire(false);
    assert.equal(view.url(), "");
    assert.equal(view.status(), "waiting");
    assert.deepEqual(view.revoked, ["blob:media-0"]);
    await view.fire(false);
    await view.fire(true);
    await view.fire(true);
    assert.equal(requests.length, 2);
    await act(async () => requests[1].finish());
    assert.equal(view.url(), "blob:media-1");
  });
});

test("leaving the viewport aborts in-flight media and ignores a late response", async t => {
  const requests = controlledFetch(t, false);
  await mounted(t, async view => {
    await view.render();
    await view.fire(true);
    await view.fire(false);
    assert.equal(requests[0].signal.aborted, true);
    await view.fire(true);
    assert.equal(requests.length, 2);
    await act(async () => requests[0].finish());
    assert.equal(view.created.length, 0);
    assert.equal(view.status(), "loading");
    await act(async () => requests[1].finish());
    assert.equal(view.status(), "ready");
  });
});

test("playing or enlarged media remains pinned offscreen without refetching on hold changes", async t => {
  const requests = controlledFetch(t);
  await mounted(t, async view => {
    await view.render();
    await view.fire(true);
    await act(async () => requests[0].finish());
    await view.render({ hold: true });
    await view.fire(false);
    assert.equal(view.url(), "blob:media-0");
    assert.equal(view.revoked.length, 0);
    assert.equal(requests.length, 1);
    await view.fire(true);
    await view.render({ hold: false });
    assert.equal(view.url(), "blob:media-0");
    assert.equal(requests.length, 1);
    await view.fire(false);
    assert.equal(view.revoked.length, 1);
    await view.render({ hold: true });
    assert.equal(requests.length, 2, "A new hold can load an offscreen item");
    await view.render({ hold: false });
    assert.equal(requests[1].signal.aborted, true);
    assert.equal(view.status(), "waiting");
  });
});

for (const change of ["token", "path", "metadata"] as const) {
  test(`${change} changes release old media and ignore stale observer callbacks`, async t => {
    const requests = controlledFetch(t);
    await mounted(t, async view => {
      await view.render();
      await view.fire(true);
      await act(async () => requests[0].finish());
      await view.render(change === "token" ? { token: "new-token" } : change === "path" ?
        { path: "sessions/other/uploads/image/preview" } : { asset: { media_type: "image/jpeg", byte_length: 3 } });
      assert.equal(view.url(), "");
      assert.deepEqual(view.revoked, ["blob:media-0"]);
      assert.equal(view.observed[0].disconnected, true);
      await view.fire(true, 0);
      await view.fire(false, 0);
      assert.equal(requests.length, 1);
      await view.fire(true);
      assert.equal(requests.length, 2);
      assert.equal(requests[1].token, change === "token" ? "new-token" : "private-token");
      assert.equal(requests[1].path, `/api/${change === "path" ? "sessions/other/uploads/image/preview" : initial.path}`);
      await view.unmount();
      assert.equal(requests[1].signal.aborted, true);
      await view.fire(true);
      assert.equal(requests.length, 2);
    });
  });
}

test("identity changes cancel old fetching even when the current item is held", async t => {
  const requests = controlledFetch(t);
  await mounted(t, async view => {
    await view.render({ hold: true });
    assert.equal(requests.length, 1);
    await view.render({ path: "sessions/other/uploads/image/preview" });
    assert.equal(requests[0].signal.aborted, true);
    assert.equal(requests.length, 2);
    await act(async () => requests[1].finish());
    assert.equal(view.status(), "ready");
  });
});

test("disabled or unauthenticated media neither observes nor loads; disabling releases ownership", async t => {
  const requests = controlledFetch(t);
  await mounted(t, async view => {
    await view.render({ token: "", hold: true });
    assert.equal(view.observed.length, 0);
    await view.retry();
    await view.render({ token: "private-token", enabled: false });
    assert.equal(view.observed.length, 0);
    assert.equal(requests.length, 0);
    await view.render({ enabled: true });
    assert.equal(requests.length, 1);
    await act(async () => requests[0].finish());
    await view.render({ enabled: false });
    assert.equal(view.url(), "");
    assert.equal(view.revoked.length, 1);
    await view.fire(true);
    await view.retry();
    assert.equal(requests.length, 1);
  });
});

for (const retry of ["button", "reentry"] as const) {
  test(`failed media retries on ${retry} without retrying on renders or repeated observer notifications`, async t => {
    let calls = 0;
    t.mock.method(globalThis, "fetch", async () => {
      calls++;
      if (calls === 1) throw new Error("Connection interrupted");
      return response();
    });
    await mounted(t, async view => {
      await view.render();
      await view.fire(true);
      assert.equal(view.status(), "error");
      assert.match(view.container.textContent!, /Connection interrupted/);
      await view.fire(true);
      await view.render({ asset: { ...initial.asset }, hold: true });
      await view.render({ hold: false });
      assert.equal(calls, 1);
      if (retry === "button") await view.retry();
      else { await view.fire(false); await view.fire(true); }
      assert.equal(calls, 2);
      assert.equal(view.status(), "ready");
    });
  });
}

test("without IntersectionObserver authenticated media loads automatically and releases on unmount", async t => {
  t.mock.method(globalThis, "fetch", async () => response());
  await mounted(t, async view => {
    await view.render();
    assert.equal(view.status(), "ready");
    assert.equal(view.observed.length, 0);
  }, false);
});

test("visible media waits for byte capacity and starts automatically when offscreen media releases it", async t => {
  const held = Array.from({ length: 3 }, () => {
    const reservation = mediaBudget.reserve(20 * 1024 * 1024);
    reservation.loaded();
    return reservation;
  });
  const requests = controlledFetch(t);
  try {
    await mounted(t, async view => {
      await view.render();
      await view.fire(true);
      assert.equal(view.status(), "waiting");
      assert.equal(requests.length, 0);
      await act(async () => held[0].release());
      assert.equal(view.status(), "loading");
      assert.equal(requests.length, 1);
      await act(async () => requests[0].finish());
      assert.equal(view.status(), "ready");
    });
  } finally { held.forEach(item => item.release()); }
});

test("offscreen queued media gives up its waiter and cannot start when capacity later becomes available", async t => {
  const held = Array.from({ length: 3 }, () => {
    const reservation = mediaBudget.reserve(20 * 1024 * 1024);
    reservation.loaded();
    return reservation;
  });
  const requests = controlledFetch(t);
  try {
    await mounted(t, async view => {
      await view.render();
      await view.fire(true);
      await view.fire(false);
      await act(async () => held[0].release());
      assert.equal(requests.length, 0);
      await view.fire(true);
      assert.equal(requests.length, 1);
      await act(async () => requests[0].finish());
    });
  } finally { held.forEach(item => item.release()); }
});

test("byte waiters remain blocked after fetch completion until retained bytes are released", async () => {
  const budget = new MediaBudget(3, 1);
  const held = budget.reserve(3);
  let arrived = false;
  const next = budget.reserveWhenReady(3, new AbortController().signal).then(result => { arrived = true; return result; });
  held.loaded();
  await Promise.resolve();
  assert.equal(arrived, false);
  assert.throws(() => budget.reserve(1), /budget is full/);
  held.release();
  const reservation = await next;
  reservation.release();
  budget.reserve(3).release();
});

test("automatic byte waiters have a finite limit and aborted waiters free their place", async () => {
  const budget = new MediaBudget(3, 1);
  const held = budget.reserve(3);
  held.loaded();
  const controllers = Array.from({ length: 16 }, () => new AbortController());
  const pending = controllers.map(controller => budget.reserveWhenReady(1, controller.signal));
  const rejected = pending.map(value => assert.rejects(value, { name: "AbortError" }));
  await assert.rejects(budget.reserveWhenReady(1, new AbortController().signal), /Too many media items/);
  controllers[0].abort();
  await rejected[0];
  const replacement = budget.reserveWhenReady(3, new AbortController().signal);
  controllers.slice(1).forEach(controller => controller.abort());
  await Promise.all(rejected);
  held.release();
  (await replacement).release();
  budget.reserve(3).release();
});

test("an item larger than the total capacity fails instead of waiting forever", async () => {
  const budget = new MediaBudget(2, 1);
  await assert.rejects(budget.reserveWhenReady(3, new AbortController().signal), /exceeds the available memory limit/);
  const aborted = new AbortController();
  aborted.abort();
  await assert.rejects(budget.reserveWhenReady(1, aborted.signal), { name: "AbortError" });
  budget.reserve(2).release();
});

test("aborting a stalled response cancels its reader and releases the byte reservation", async t => {
  const budget = new MediaBudget(3, 1), controller = new AbortController();
  let cancelled = false;
  let reading: () => void = () => {};
  const ready = new Promise<void>(resolve => { reading = resolve; });
  t.mock.method(globalThis, "fetch", async () => new Response(new ReadableStream<Uint8Array>({
    pull() { reading(); },
    cancel() { cancelled = true; },
  }), { headers: { "Content-Type": "image/png" } }));
  const created = t.mock.method(URL, "createObjectURL", () => assert.fail("Aborted content cannot become an object URL"));
  const pending = loadMedia("private-token", initial.path, initial.asset, controller.signal, budget, true);
  await ready;
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(cancelled, true);
  assert.equal(created.mock.callCount(), 0);
  budget.reserve(3).release();
});
