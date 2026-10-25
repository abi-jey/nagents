import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { TeamTree } from "../team/TeamTree.js";
import { taskAgent, type TeamAgent, type TeamProfile } from "../team/model.js";
import type { RetainedTask } from "../../types.js";
import { profileTarget } from "../team/useTeam.js";

const task = (id: string, parent = "", updated = 1, status: RetainedTask["status"] = "running"): RetainedTask => ({
  session_id: id, title: id, parent_session_id: parent, status, result: "", error: "", prompt: `${id} task`, root_session_id: "root",
    depth: parent ? 2 : 1, profile: "assistant", mode: "build",
  followups: 0, activation: 0, trigger: "delegation", provider: "my-openai", provider_kind: "openai", model: "test-model",
  updated_at: updated, started_at: updated, phase: status === "completed" ? "done" : "running", can_message: true,
});
const profile = (id: string, name = id, provider = "openai"): TeamProfile => ({ id, name, description: name,
  provider, model: "test-model", used: true, available: true, can_start: true });
const main = taskAgent({ ...task("main"), session_id: "", title: "Assistant", profile: "", phase: "ready" });
const agents = (...items: RetainedTask[]): TeamAgent[] => [main, ...items.map(taskAgent)];
const renderMarkup = (items: TeamAgent[], profiles: TeamProfile[], selected = "") => renderToStaticMarkup(createElement(TeamTree, {
  agents: items, profiles, selected, inspect() {}, configure() {}, arrivals: {}, error: "", refresh() {},
}));

test("definition groups are scoped to each parent and unused profiles stay in the visible definition list", () => {
  const items = agents(
    { ...task("parent-a", "", 10), title: "Parent A", profile: "assistant" },
    { ...task("parent-b", "", 9), title: "Parent B", profile: "assistant" },
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
  assert.deepEqual([...dom.window.document.querySelectorAll<HTMLElement>(".team-profiles [data-profile-id]")].map(row => row.dataset.profileId), ["writer", "unused"]);
  assert.equal(dom.window.document.querySelector(".team-run-search"), null);
  assert.equal(dom.window.document.querySelector(".team-start-agent"), null);
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

test("visible definitions keep parent policy, configuration access and manual group collapse", async t => {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true, requestAnimationFrame: (callback: FrameRequestCallback) => { callback(0); return 0; },
    cancelAnimationFrame() {}, matchMedia: () => ({ matches: true, addEventListener() {}, removeEventListener() {} }) });
  t.after(() => {
    for (const key of Reflect.ownKeys(globalThis)) if (!(key in previous)) Reflect.deleteProperty(globalThis, key);
    Object.defineProperties(globalThis, previous);
    dom.window.close();
  });
  const react = createRoot(dom.window.document.getElementById("root")!);
  const parent = { ...task("parent", "", 10), title: "Parent", profile: "assistant", can_start_children: true, child_agents: ["worker"] };
  const past = { ...task("old-worker", "parent", 2, "completed"), title: "Archived worker", profile: "worker" };
  const running = { ...task("current-worker", "parent", 12), title: "Current worker", profile: "worker" };
  const opened: string[] = [], configured: string[] = [];
  let selected = "parent";
  const view = () => createElement(TeamTree, { agents: agents(parent, running, past),
    profiles: [profile("worker", "Worker"), { ...profile("unused", "Unused"), used: false }], selected,
    inspect(id) { opened.push(id); }, configure(id) { configured.push(id); }, arrivals: {}, error: "", refresh() {} });
  await act(async () => react.render(view()));
  const group = () => dom.window.document.querySelector<HTMLElement>('.team-definition-group[data-parent-id="parent"][data-profile="worker"]')!;
  const toggle = group().querySelector<HTMLButtonElement>(".team-definition-toggle")!;
  const runningRow = dom.window.document.querySelector('[data-agent-id="current-worker"]');
  assert.ok(runningRow);
  await act(async () => toggle.click());
  assert.equal(toggle.getAttribute("aria-expanded"), "false");
  const definition = (id: string) => dom.window.document.querySelector<HTMLElement>(`.team-profiles [data-profile-id="${id}"]`)!;
  assert.match(definition("worker").textContent || "", /1 active/);
  assert.match(definition("unused").textContent || "", /Not started/);
  assert.equal(dom.window.document.querySelector('input[type="search"]'), null);
  assert.equal(dom.window.document.querySelector(".team-start-agent"), null);
  await act(async () => definition("worker").querySelector<HTMLButtonElement>(".team-profile")!.click());
  assert.equal(opened.at(-1), profileTarget("worker", "parent"));
  selected = opened.at(-1)!;
  await act(async () => react.render(view()));
  assert.equal(group().querySelector(".team-definition-toggle")?.getAttribute("aria-expanded"), "false",
    "choosing a definition does not reopen a manually collapsed child group");
  assert.equal(dom.window.document.querySelector('[data-agent-id="current-worker"]'), runningRow);
  const unavailable = definition("unused").querySelector<HTMLButtonElement>(".team-profile")!;
  assert.equal(unavailable.disabled, false, "an unavailable definition remains inspectable");
  assert.match(unavailable.getAttribute("aria-description") || "", /parent's policy/);
  await act(async () => unavailable.click());
  assert.equal(opened.at(-1), profileTarget("unused", "parent"), "changing definitions retains the selected parent");
  await act(async () => definition("unused").querySelector<HTMLButtonElement>(".team-profile-settings")!.click());
  assert.equal(configured.at(-1), profileTarget("unused"));
  await act(async () => react.unmount());
});
