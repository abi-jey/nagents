import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import type { ResponseMetadata } from "./responseMetadata.js";
import type { DelegationPresentation } from "./delegationFlow.js";
import type { Entry } from "./transcript.js";
import { delegationRelayState, relayAppears, RelayMotion, type RelayState } from "./relayActivity.js";
import { RelayActivityMark } from "./RelayActivityMark.js";
import { SlidingActivityText } from "./SlidingActivityText.js";

const metadata: ResponseMetadata = {
  response_id: "response", response_session_id: "child", run_id: "child-run", revision: 1,
  round_number: 0, history_id: "", status: "thinking", summary: "The child is reasoning publicly.",
  summary_truncated: false, summary_expired: false, usage_reported: false,
};

function presentation(overrides: Partial<DelegationPresentation> = {}): DelegationPresentation {
  return { kind: "start", taskId: "child", name: "small cedar", profile: "mini", phase: "running",
    prompt: "Check the path", text: "", sources: [], ...overrides };
}

function source(id: string, values: Partial<Entry> = {}): Entry {
  return { id, kind: "tool", text: "", title: "delegate", state: "Starting", ...values };
}

test("a fresh pending delegation starts explicitly and only fresh start evidence appears", () => {
  const pending = presentation({ taskId: "", sources: [source("call")] });
  assert.equal(delegationRelayState(pending), "starting");
  assert.equal(relayAppears(pending), true);
  for (const state of ["Preparing", "Requested"]) {
    const fresh = presentation({ taskId: "", sources: [source(`fresh-${state}`, { state })] });
    assert.equal(delegationRelayState(fresh), "starting");
    assert.equal(relayAppears(fresh), true);
  }
  const archived = presentation({ taskId: "", sources: [source("archived-call", { state: "Recorded result", recorded: true })] });
  assert.equal(delegationRelayState(archived), "ready", "an unlinked archived acknowledgement is settled without host task identity");
  assert.equal(relayAppears(archived), false);
  const completed = presentation({ taskId: "", sources: [source("completed-call", { state: "Completed" })] });
  assert.equal(delegationRelayState(completed), "ready");
  assert.equal(relayAppears(completed), false);
  assert.equal(relayAppears(presentation({ kind: "result", sources: [source("call")] })), false);
  assert.equal(relayAppears(presentation({ phase: "done", sources: [source("call")] })), false);
  assert.equal(relayAppears(presentation({ sources: [source("saved", { state: "Recorded result", recorded: true })] })), false);
  assert.equal(relayAppears(presentation({ sources: [] })), false, "activity without a source tool has no arrival cue");
});

test("the relay maps public reasoning, writing, tools, waits, and terminal states", () => {
  assert.equal(delegationRelayState(presentation({ latest: { id: "reason", kind: "assistant", text: "", responseMetadata: metadata } })), "reasoning");
  assert.equal(delegationRelayState(presentation({ latest: { id: "write", kind: "assistant", text: "Answer chunk" } })), "writing");
  assert.equal(delegationRelayState(presentation({ latest: { id: "tool", kind: "tool", text: "", title: "search" } })), "tool");
  assert.equal(delegationRelayState(presentation({ phase: "waiting" })), "waiting");
  assert.equal(delegationRelayState(presentation({ phase: "done" })), "done");
  assert.equal(delegationRelayState(presentation({ phase: "failed" })), "failed");
  assert.equal(delegationRelayState(presentation({ phase: "cancelled" })), "cancelled");
  assert.equal(delegationRelayState(presentation({ phase: "ready" })), "ready");
});

test("relay motion retargets continuously, settles immediately for reduced motion, and stops at terminal states", () => {
  const motion = new RelayMotion("starting");
  motion.sample(100, false);
  motion.transition("writing", 100);
  const beforeRetarget = motion.sample(360, false);
  assert.equal(beforeRetarget.animate, true);
  motion.transition("tool", 360);
  const firstRetargetFrame = motion.sample(360, false);
  assert.deepEqual(firstRetargetFrame.points, beforeRetarget.points, "retargeting begins at the exact rendered geometry");
  assert.equal(firstRetargetFrame.animate, true);

  const reduced = motion.sample(360, true);
  assert.equal(reduced.animate, false);
  assert.equal(motion.sample(361, true).animate, false, "reduced motion leaves no continuing frame loop");

  motion.transition("done", 400);
  const terminal = motion.sample(1_000, false);
  assert.equal(terminal.animate, false, "settled terminal marks do not keep animating");
});

function browserFixture() {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document,
    HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true });
  let nextFrame = 1;
  const frames = new Map<number, FrameRequestCallback>();
  dom.window.requestAnimationFrame = (callback: FrameRequestCallback) => {
    const handle = nextFrame++;
    frames.set(handle, callback);
    return handle;
  };
  dom.window.cancelAnimationFrame = (handle: number) => { frames.delete(handle); };
  dom.window.matchMedia = (media: string) => ({ matches: false, media, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => true }) as MediaQueryList;
  const animationCalls: unknown[][] = [];
  const animateDescriptor = Object.getOwnPropertyDescriptor(dom.window.HTMLElement.prototype, "animate");
  Object.defineProperty(dom.window.HTMLElement.prototype, "animate", { configurable: true, value: (...args: unknown[]) => {
    animationCalls.push(args);
    return { cancel() {} } as Animation;
  } });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const render = (state: RelayState, text: string, activity: string, appear = false) => act(async () => root.render(createElement("div", null,
    createElement(RelayActivityMark, { state, appear }), createElement(SlidingActivityText, { text, activity }))));
  return { dom, container, animationCalls, frames, render, async close() {
    await act(async () => root.unmount());
    dom.window.close();
    if (animateDescriptor) Object.defineProperty(dom.window.HTMLElement.prototype, "animate", animateDescriptor);
    else Reflect.deleteProperty(dom.window.HTMLElement.prototype, "animate");
    for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT"] as const) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  } };
}

test("token chunks preserve the row, SVG nodes, sliding span, and text node without replaying entrance opacity", async () => {
  const ui = browserFixture();
  try {
    await ui.render("running", "token-0", "assistant:response-1");
    assert.equal(ui.animationCalls.length, 0, "mounting the first activity does not animate its text");
    const row = ui.container.firstElementChild;
    const mark = ui.container.querySelector(".relay-activity-mark");
    const svg = mark?.querySelector("svg");
    const nodes = [...(svg?.querySelectorAll(".relay-nodes circle") || [])];
    const edges = [...(svg?.querySelectorAll(".relay-edges path") || [])];
    const packets = [...(svg?.querySelectorAll(".relay-packets circle") || [])];
    const viewport = ui.container.querySelector<HTMLElement>(".delegation-tail")!;
    const entrance = viewport.firstElementChild;
    const content = viewport.querySelector(".delegation-tail-content");
    const textNode = content?.firstChild;
    assert.ok(textNode);
    assert.equal(textNode.nodeType, 3, "the preserved child is the actual text node");

    for (let index = 1; index <= 115; index++) {
      await ui.render("running", `token-${index}`, "assistant:response-1");
      assert.equal(ui.container.firstElementChild, row);
      assert.equal(ui.container.querySelector(".relay-activity-mark"), mark);
      assert.equal(mark?.querySelector("svg"), svg);
      assert.deepEqual([...svg!.querySelectorAll(".relay-nodes circle")], nodes);
      assert.deepEqual([...svg!.querySelectorAll(".relay-edges path")], edges);
      assert.deepEqual([...svg!.querySelectorAll(".relay-packets circle")], packets);
      assert.equal(ui.container.querySelector(".delegation-tail"), viewport);
      assert.equal(viewport.firstElementChild, entrance);
      assert.equal(viewport.querySelector(".delegation-tail-content"), content);
      assert.equal(content?.firstChild, textNode);
      assert.equal((content?.textContent || "").trim(), `token-${index}`);
      assert.notEqual(content?.getAttribute("style")?.includes("opacity: 0"), true);
    }
    assert.equal(ui.animationCalls.length, 0, "token changes do not replay a mount entrance animation");
    assert.ok(ui.frames.size <= 2, "one pending frame per animated mark/text at most");

    for (const [text, activity] of [
      ["", "assistant:response-2"], [" ", "assistant:response-2"],
      ["", "assistant:response-2"], ["restored words", "assistant:response-2"],
      ["tool output", "tool:call-1"],
    ] as const) {
      await ui.render("running", text, activity);
      assert.equal(ui.container.querySelector(".delegation-tail-content")?.firstChild, textNode,
        "empty, whitespace and semantic activity changes retain the same text node");
      assert.equal((textNode as Text).data, text.replace(/\s+/g, " ").trim());
    }
    assert.equal(ui.animationCalls.length, 2, "only semantic activity changes trigger entrance motion");
    for (const [frames, timing] of ui.animationCalls) {
      assert.ok(Array.isArray(frames));
      assert.ok((frames as Array<Record<string, unknown>>).every(frame => Object.keys(frame).every(key => key === "transform")));
      assert.equal((timing as { duration?: number }).duration, 160);
    }
    assert.equal((textNode as Text).data, "tool output");

    await ui.render("done", "final result", "result");
    assert.equal(mark?.getAttribute("data-state"), "done", "completion overrides any arrival appearance immediately");
    assert.equal(ui.container.querySelector(".delegation-tail-content")?.firstChild, textNode,
      "terminal text still uses the mounted text node");
  } finally {
    await ui.close();
    assert.equal(ui.frames.size, 0, "unmount cancels every scheduled animation frame");
  }
});

test("completed history mounts at rest without mark frames or a text entrance", async () => {
  const ui = browserFixture();
  try {
    await ui.render("done", "Archived result", "result");
    assert.equal(ui.container.querySelector<HTMLElement>(".relay-activity-mark")?.dataset.state, "done");
    assert.equal(ui.container.querySelector(".delegation-tail-content")?.textContent, "Archived result");
    assert.equal(ui.animationCalls.length, 0, "cold history does not play a text entrance");
    assert.equal(ui.frames.size, 0, "a completed mark and zero-overflow text schedule no frames");
  } finally {
    await ui.close();
    assert.equal(ui.frames.size, 0, "completed rows remain idle after unmount");
  }
});

test("the fresh appearance cue runs once and is not replayed by later content updates", async () => {
  const ui = browserFixture();
  try {
    await ui.render("starting", "starting", "assignment", true);
    const mark = ui.container.querySelector<HTMLElement>(".relay-activity-mark")!;
    assert.equal(mark.dataset.state, "appearing");
    await ui.render("starting", "more details", "assignment", true);
    assert.equal(ui.container.querySelector(".relay-activity-mark"), mark);
    await act(async () => new Promise(resolve => setTimeout(resolve, 210)));
    assert.equal(mark.dataset.state, "starting", "the arrival cue settles into the explicit starting state once");
    await ui.render("starting", "additional token", "assignment", true);
    assert.equal(mark.dataset.state, "starting", "the same activity does not replay appearance");
  } finally { await ui.close(); }
});
