import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { TeamTree } from "../team/TeamTree.js";
import { taskAgent, type TeamAgent, type TeamProfile } from "../team/model.js";
import type { RetainedTask } from "../../types.js";

const task = (id: string, parent = "", updated = 1, status: RetainedTask["status"] = "running"): RetainedTask => ({
  id, name: id, parent_task_id: parent, status, result: "", error: "", prompt: `${id} task`, session_id: "root",
  child_session_id: `child-${id}`, parent_session_id: "root", depth: parent ? 2 : 1, profile: "assistant", mode: "build",
  followups: 0, activation: 0, trigger: "delegation", provider: "my-openai", provider_kind: "openai", model: "test-model",
  updated_at: updated, started_at: updated, phase: status === "completed" ? "done" : "running", can_message: true,
});
const profile = (id: string, name = id, provider = "openai"): TeamProfile => ({ id, name, description: name,
  provider, model: "test-model", used: true, available: true, can_start: true });
const main = taskAgent({ ...task("main"), id: "", name: "Assistant", profile: "", phase: "ready" });
const agents = (...items: RetainedTask[]): TeamAgent[] => [main, ...items.map(taskAgent)];
const renderMarkup = (items: TeamAgent[], profiles: TeamProfile[], selected = "") => renderToStaticMarkup(createElement(TeamTree, {
  agents: items, profiles, selected, inspect() {}, configure() {}, arrivals: {}, error: "", refresh() {},
}));

test("definition groups are scoped to each parent and unused profiles stay in the selector", () => {
  const items = agents(
    { ...task("parent-a", "", 10), name: "Parent A", profile: "assistant" },
    { ...task("parent-b", "", 9), name: "Parent B", profile: "assistant" },
    { ...task("a-old", "parent-a", 10), profile: "writer" },
    { ...task("a-new", "parent-a", 20), profile: "writer" },
    { ...task("b-only", "parent-b", 15), profile: "writer" },
  );
  const dom = new JSDOM(renderMarkup(items, [profile("writer", "Writer"), profile("unused", "Unused")]));
  const groups = [...dom.window.document.querySelectorAll<HTMLElement>('.team-definition-group[data-profile="writer"]')];
  assert.equal(groups.length, 2);
  assert.deepEqual(groups.map(group => group.dataset.parentId), ["parent-a", "parent-b"]);
  const parentList = groups[0]!.parentElement!;
  assert.deepEqual([...parentList.querySelectorAll<HTMLElement>('[data-agent-id]')].map(row => row.dataset.agentId).filter(id => id?.startsWith("a-")), ["a-new", "a-old"]);
  assert.equal(dom.window.document.querySelector('.team-definition-group[data-profile="unused"]'), null);
  assert.deepEqual([...dom.window.document.querySelectorAll<HTMLSelectElement>(".team-start-agent select option")].map(option => option.value), ["writer", "unused"]);
});

test("A2A connection chip appears once per parent and definition group", () => {
  const items = agents(
    { ...task("p1", "", 4), profile: "assistant" }, { ...task("p2", "", 3), profile: "assistant" },
    { ...task("r1", "p1", 2), profile: "remote", provider_kind: "a2a", provider: "remote" },
    { ...task("r2", "p1", 1), profile: "remote", provider_kind: "a2a", provider: "remote" },
    { ...task("r3", "p2", 1), profile: "remote", provider_kind: "a2a", provider: "remote" },
  );
  const dom = new JSDOM(renderMarkup(items, [profile("remote", "Remote", "a2a")]));
  for (const parentId of ["p1", "p2"]) {
    const group = dom.window.document.querySelector<HTMLElement>(`.team-definition-group[data-parent-id="${parentId}"][data-profile="remote"]`);
    assert.ok(group);
    assert.equal(group.querySelectorAll(".remote-connection-chip").length, 1);
    assert.equal(group.querySelectorAll(".team-agent-row .remote-connection-chip").length, 0);
  }
});

test("search reveals matching past descendants and clearing it restores manual collapses", async t => {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    Event: dom.window.Event, HTMLInputElement: dom.window.HTMLInputElement,
    IS_REACT_ACT_ENVIRONMENT: true, requestAnimationFrame: (callback: FrameRequestCallback) => { callback(0); return 0; },
    cancelAnimationFrame() {}, matchMedia: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });
  t.after(() => {
    for (const key of Reflect.ownKeys(globalThis)) if (!(key in previous)) Reflect.deleteProperty(globalThis, key);
    Object.defineProperties(globalThis, previous);
    dom.window.close();
  });
  const react = createRoot(dom.window.document.getElementById("root")!);
  const parent = { ...task("parent", "", 10), name: "Parent", profile: "assistant" };
  const past = { ...task("old-worker", "parent", 2, "completed"), name: "Archive match", profile: "worker" };
  const running = { ...task("current-worker", "parent", 12), name: "Current worker", profile: "worker" };
  const view = () => createElement(TeamTree, { agents: agents(parent, running, past), profiles: [profile("worker", "Worker")], selected: "",
    inspect() {}, configure() {}, arrivals: {}, error: "", refresh() {} });
  await act(async () => react.render(view()));
  const group = () => dom.window.document.querySelector<HTMLElement>('.team-definition-group[data-parent-id="parent"][data-profile="worker"]')!;
  const toggle = group().querySelector<HTMLButtonElement>(".team-definition-toggle")!;
  await act(async () => toggle.click());
  assert.equal(toggle.getAttribute("aria-expanded"), "false", "past definition groups start collapsed and manual collapse is retained");
  const input = dom.window.document.querySelector<HTMLInputElement>('input[aria-label="Find an agent or run"]')!;
  const setInput = async (value: string) => {
    input.value = value;
    const propsKey = Object.keys(input).find(key => key.startsWith("__reactProps$"));
    assert.ok(propsKey, "React attaches the search change handler");
    const props = (input as unknown as Record<string, { onChange?: (event: { target: HTMLInputElement }) => void }>)[propsKey!];
    await act(async () => props.onChange?.({ target: input }));
  };
  await setInput("Archive match");
  assert.equal(group().hidden, false, "search reveals the matching ancestor group");
  assert.equal(group().parentElement!.querySelector<HTMLButtonElement>('[data-bucket-heading="past"] button')?.getAttribute("aria-expanded"), "true");
  const result = dom.window.document.querySelector<HTMLElement>('[data-agent-id="old-worker"]');
  assert.ok(result && !result.closest(".team-tree-branch")!.hasAttribute("hidden"), `matching past child is revealed (query=${input.value}; ${result?.closest(".team-tree-branch")?.outerHTML.slice(0, 180)})`);
  await setInput("");
  assert.equal(group().querySelector<HTMLButtonElement>(".team-definition-toggle")!.getAttribute("aria-expanded"), "false",
    "clearing search restores the user's explicit collapse choice");
  await act(async () => react.unmount());
});
