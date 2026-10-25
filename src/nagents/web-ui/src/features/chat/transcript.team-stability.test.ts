import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { TeamTree } from "../team/TeamTree.js";
import type { TeamAgent } from "../team/model.js";

const agent = (id: string, parentId = "", phase: TeamAgent["phase"] = "running"): TeamAgent => ({
  id, parentId, phase, name: id || "Assistant", profile: id ? "assistant" : "", prompt: "Check the fixture.",
  reason: "", provider: "openai", providerKind: "openai", model: "test", startedAt: id ? 1 : 0,
  updatedAt: 1, canMessage: true, messageReason: "",
});
type Motion = {
  row: HTMLElement; x: number; y: number; cancelled: boolean; finished: boolean;
  onfinish?: () => void; cancel: () => void;
};

function fixture({ strict = false, movesBlurFocus = false }: { strict?: boolean; movesBlurFocus?: boolean } = {}) {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: (callback: FrameRequestCallback) => dom.window.setTimeout(() => callback(0), 0),
    cancelAnimationFrame: (handle: number) => dom.window.clearTimeout(handle) });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const layout = new Map<string, { x: number; y: number }>([["main", { x: 0, y: 0 }], ["worker", { x: 0, y: 100 }]]);
  const arrival = new Map<string, number>();
  const motions: Motion[] = [];
  let reduced = false, peak = 0;
  const active = (row: HTMLElement) => motions.filter(motion => motion.row === row && !motion.cancelled && !motion.finished);
  Object.defineProperty(dom.window, "matchMedia", { value: () => ({ matches: reduced }) });
  const prototype = dom.window.HTMLElement.prototype;
  const insertBefore = prototype.insertBefore;
  // Model browsers clearing native focus while React moves an existing row.
  if (movesBlurFocus) Object.defineProperty(prototype, "insertBefore", { value(this: HTMLElement, node: Node, child: Node | null) {
    const focused = dom.window.document.activeElement;
    if (node.parentNode && focused instanceof dom.window.HTMLElement && node.contains(focused)) focused.blur();
    return insertBefore.call(this, node, child);
  } });
  const visible = (element: HTMLElement) => !element.closest("[hidden]");
  Object.defineProperties(prototype, {
    clientHeight: { get(this: HTMLElement) { return this.classList.contains("team-tree-scroll") ? 600 : 40; } },
    scrollTo: { value(this: HTMLElement, options: ScrollToOptions) { this.scrollTop = options.top || 0; } },
    offsetParent: { get(this: HTMLElement) { return this.dataset.agentId ? this.closest(".team-tree-scroll") : null; } },
    offsetTop: { get(this: HTMLElement) { return this.dataset.agentId ? layout.get(this.dataset.agentId)?.y || 0 : this.classList.contains("team-tree-scroll") ? 100 : 0; } },
    offsetLeft: { get(this: HTMLElement) { return this.dataset.agentId ? layout.get(this.dataset.agentId)?.x || 0 : this.classList.contains("team-tree-scroll") ? 20 : 0; } },
    offsetWidth: { get(this: HTMLElement) { return visible(this) ? 180 : 0; } },
    offsetHeight: { get(this: HTMLElement) { return visible(this) ? 40 : 0; } },
    getBoundingClientRect: { value(this: HTMLElement) {
      if (!visible(this)) return new dom.window.DOMRect();
      if (!this.dataset.agentId) return new dom.window.DOMRect(20, 100, 240, 600);
      const point = layout.get(this.dataset.agentId) || { x: 0, y: 0 };
      const motion = active(this).at(-1);
      const scroll = this.closest<HTMLElement>(".team-tree-scroll")?.scrollTop || 0;
      return new dom.window.DOMRect(20 + point.x + (motion?.x ?? arrival.get(this.dataset.agentId) ?? 0),
        100 + point.y - scroll + (motion?.y || 0), 180, 40);
    } },
    animate: { value(this: HTMLElement, frames: Keyframe[]) {
      const transform = String(frames[0].transform);
      const parts = /translate\(([-.\d]+)px, ([-.\d]+)px\)/.exec(transform);
      assert.ok(parts, "positional motion has finite translation coordinates");
      const motion: Motion = { row: this, x: Number(parts[1]), y: Number(parts[2]), cancelled: false, finished: false,
        cancel() { this.cancelled = true; } };
      motions.push(motion); peak = Math.max(peak, active(this).length);
      return motion;
    } },
  });
  const render = (agents: TeamAgent[], arrivals = {}) => act(async () => {
    const tree = createElement(TeamTree, { agents, profiles: [], selected: "", inspect() {}, configure() {}, arrivals,
      error: "", refresh() {} });
    root.render(strict ? createElement(StrictMode, null, tree) : tree);
  });
  return { dom, container, render, layout, arrival, motions, active, peak: () => peak,
    reduce: () => { reduced = true; },
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT", "requestAnimationFrame", "cancelAnimationFrame"])
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
    } };
}

test("Team arrival transforms do not become positional layout changes during stream updates", async () => {
  const ui = fixture();
  const agents = [agent(""), agent("worker")];
  ui.arrival.set("worker", -9);
  try {
    await ui.render(agents, { worker: Date.now() });
    ui.arrival.set("worker", -3);
    await ui.render(agents.map(value => ({ ...value, reason: "More streamed text" })), { worker: Date.now() });
    assert.equal(ui.motions.length, 0, "CSS arrival movement must not trigger a competing FLIP animation");
    assert.ok(ui.container.querySelector('[data-agent-id="worker"]')?.classList.contains("is-arriving"));
  } finally { await ui.close(); }
});

test("Team cancels only its positional animation when a row hides or the tree unmounts", async () => {
  const ui = fixture();
  const agents = [agent(""), agent("worker")], arrivals = { worker: Date.now() };
  try {
    await ui.render(agents, arrivals);
    const row = ui.container.querySelector<HTMLElement>('[data-agent-id="worker"]')!;
    ui.layout.set("worker", { x: 0, y: 160 });
    await ui.render(agents.map(value => ({ ...value })), arrivals);
    assert.equal(ui.active(row).length, 1);
    const toggle = ui.container.querySelector<HTMLButtonElement>(".team-definition-toggle")!;
    await act(async () => toggle.click());
    assert.equal(ui.active(row).length, 0);
    assert.equal(ui.motions[0].cancelled, true);
    assert.ok(row.classList.contains("is-arriving"), "CSS spawn and state animations remain intact");
    await act(async () => toggle.click());
    ui.layout.set("worker", { x: 0, y: 190 });
    await ui.render(agents.map(value => ({ ...value })), arrivals);
    assert.equal(ui.motions.length, 2);
    assert.equal(ui.active(row).length, 1);
  } finally { await ui.close(); }
  assert.equal(ui.motions[1].cancelled, true, "unmount releases the tree's remaining positional motion");
});

for (const strict of [false, true]) test(`Team positional motion stays bounded across repeated in-flight updates${strict ? " in StrictMode" : ""}`, async () => {
  const ui = fixture({ strict });
  const agents = [agent(""), agent("worker")];
  try {
    await ui.render(agents);
    const row = ui.container.querySelector<HTMLElement>('[data-agent-id="worker"]')!;
    ui.layout.set("worker", { x: 0, y: 160 });
    await ui.render(agents.map(value => ({ ...value, reason: "Layout changed" })));
    assert.equal(ui.motions.length, 1);
    assert.equal(ui.motions[0].y, -60);
    for (let index = 0; index < 30; index++)
      await ui.render(agents.map(value => ({ ...value, reason: `Streaming update ${index}` })));
    assert.equal(ui.motions.length, 1, "an in-flight transform must not feed back into the next layout measurement");
    ui.layout.set("worker", { x: 0, y: 190 });
    await ui.render(agents.map(value => ({ ...value, reason: "Another layout change" })));
    assert.equal(ui.motions.length, 2);
    assert.equal(ui.motions[0].cancelled, true);
    assert.equal(ui.motions[1].y, -30);
    assert.equal(ui.active(row).length, 1);
    assert.equal(ui.peak(), 1, "each row owns at most one positional animation");
    ui.reduce();
    await ui.render(agents.map(value => ({ ...value, reason: "Reduced motion enabled" })));
    assert.equal(ui.active(row).length, 0);
    assert.equal(ui.motions.length, 2);
  } finally { await ui.close(); }
});

test("Team completion preserves revealed branches, nested rows and actual keyboard focus", async () => {
  const ui = fixture({ movesBlurFocus: true });
  const agents = [agent(""), { ...agent("worker"), startedAt: 2 }, agent("other"), agent("nested", "worker"), agent("old-past", "", "done")];
  try {
    await ui.render(agents);
    const worker = ui.container.querySelector<HTMLElement>('[data-agent-id="worker"]')!;
    const nested = ui.container.querySelector<HTMLElement>('[data-agent-id="nested"]')!;
    const group = ui.container.querySelector('.team-definition-group[data-parent-id=""]');
    const past = ui.container.querySelector<HTMLButtonElement>('.team-run-bucket[data-parent-id=""][data-bucket-heading="past"] button')!;
    await act(async () => past.click());
    await act(async () => past.click());
    assert.equal(past.getAttribute("aria-expanded"), "false");
    const button = worker.querySelector<HTMLButtonElement>(".team-agent-open")!;
    button.focus();
    await ui.render(agents.map(value => ["worker", "nested"].includes(value.id) ? { ...value, phase: "done" } : value));
    assert.equal(ui.container.querySelector('[data-agent-id="worker"]'), worker);
    assert.equal(ui.container.querySelector('[data-agent-id="nested"]'), nested);
    assert.equal(ui.container.querySelector('.team-definition-group[data-parent-id=""]'), group);
    assert.equal(worker.closest("[hidden]"), null, "a focused running row remains visible when it moves to Past");
    assert.equal(nested.closest("[hidden]"), null, "a previously revealed child subtree remains open after completion");
    assert.equal(ui.dom.window.document.activeElement, button);
    assert.equal(past.getAttribute("aria-expanded"), "true", "moving keyboard focus reveals its destination bucket");
    button.blur();
    await ui.render(agents.map(value => ["worker", "nested"].includes(value.id) ? { ...value, phase: "done", reason: "Later poll" } : value));
    assert.equal(worker.closest("[hidden]"), null, "the revealed destination remains open after keyboard focus moves away");
    past.focus();
    await act(async () => past.click());
    assert.ok(worker.closest("[hidden]"), "an explicit collapse after focus leaves the row remains effective");
    assert.equal(ui.dom.window.document.activeElement, past);
  } finally { await ui.close(); }
});

test("Team completion retains revealed expansion without focus while explicit collapses stay in force", async () => {
  const ui = fixture();
  let agents = [agent(""), agent("worker"), agent("nested", "worker")];
  try {
    await ui.render(agents);
    const worker = ui.container.querySelector<HTMLElement>('[data-agent-id="worker"]')!;
    const nested = ui.container.querySelector<HTMLElement>('[data-agent-id="nested"]')!;
    agents = agents.map(value => value.id ? { ...value, phase: "done" } : value);
    await ui.render(agents);
    assert.equal(worker.closest("[hidden]"), null);
    assert.equal(nested.closest("[hidden]"), null);
    const toggle = ui.container.querySelector<HTMLButtonElement>('.team-definition-group[data-parent-id=""] .team-definition-toggle')!;
    await act(async () => toggle.click());
    assert.ok(worker.closest("[hidden]"));
    await ui.render(agents.map(value => ({ ...value, reason: "Polling update" })));
    assert.ok(worker.closest("[hidden]"), "polling cannot override a user's explicit collapse");
  } finally { await ui.close(); }
});

test("cold past Team runs remain lazy until their group and bucket are explicitly opened", async () => {
  const ui = fixture();
  const agents = [agent(""), ...Array.from({ length: 8 }, (_, index) => agent(`past-${index}`, "", "done"))];
  try {
    await ui.render(agents);
    assert.equal(ui.container.querySelectorAll('.team-tree-branch[data-bucket="past"]').length, 0);
    const group = ui.container.querySelector<HTMLButtonElement>(".team-definition-toggle")!;
    await act(async () => group.click());
    assert.equal(ui.container.querySelectorAll('.team-tree-branch[data-bucket="past"]').length, 0);
    const bucket = ui.container.querySelector<HTMLButtonElement>('.team-run-bucket[data-bucket-heading="past"] button')!;
    await act(async () => bucket.click());
    assert.equal(ui.container.querySelectorAll('.team-tree-branch[data-bucket="past"]').length, 2);
    assert.match(ui.container.querySelector(".team-group-more")?.textContent || "", /Show 6 more/);
  } finally { await ui.close(); }
});
