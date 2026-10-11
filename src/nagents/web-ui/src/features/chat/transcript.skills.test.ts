import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, type ComponentProps } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { commandIntent, commandMatches, skillCommand } from "./commands.js";
import { Composer } from "./Composer.js";
import { SkillsDialog } from "./SkillsDialog.js";
import { useSkills, type SkillItem, type SkillsState } from "./skills.js";
import { MessageQueue, queueMessage } from "../../api/messages.js";

const skill = (name: string, description = "Review a workspace", enabled = true): SkillItem => ({ name, description, enabled, command: name, canonical_command: `skill:${name}` });
const items = [skill("review"), skill("help", "Skill sharing a built-in command"), skill("_2Review"), skill("off", "Disabled", false)];

test("skill invocations preserve literal multiline input and local builtins keep precedence", () => {
  const invocation = " /_2Review  Keep  spacing\n\nAnd this line\t ";
  assert.deepEqual(commandIntent(invocation, true), { name: "skill", invocation });
  assert.deepEqual(commandIntent("/skill:help explain this", true), { name: "skill", invocation: "/skill:help explain this" });
  assert.deepEqual(commandIntent("/help", true), { name: "help", argument: "" });
  assert.deepEqual(commandIntent("/skills review", true), { name: "skills", argument: "review" });
  assert.deepEqual(commandIntent("/resume root", true), { name: "sessions", argument: "root" });
  assert.equal(commandIntent("Use /review here", true), undefined);
  assert.throws(() => commandIntent("/login surprise", true), /Usage/);
  assert.throws(() => commandIntent("/skill:bad:name", true), /Unknown browser command/);
  assert.throws(() => commandIntent(`/${"x".repeat(65)}`, true), /Unknown browser command/);
  assert.equal(skillCommand(skill("voice")), "skill:voice");
  assert.equal(skillCommand(skill("resume")), "skill:resume");
  assert.equal(skillCommand(skill("Voice")), "Voice", "Skill names remain case-sensitive");
  assert.deepEqual(commandMatches("/_2", items).map(item => item.name), ["_2Review"]);
  assert.deepEqual(commandMatches("/skill:r", items).map(item => item.name), ["skill:review"]);
  assert.deepEqual(commandMatches("/help", items).map(item => item.name), ["help"]);
  assert.ok(commandMatches("/", items).some(item => item.name === "skill:help" && item.skill === "help"));
  assert.ok(!commandMatches("/", items).some(item => item.name === "off"));
});

test("queued skill requests retain raw text and a distinct retry identity from ordinary prompts", async t => {
  const queue = new MessageQueue(), invocation = "/review  first\nsecond\n";
  const message = queue.prepare("root", invocation, () => "skill-id", [], "skill");
  assert.equal(queue.prepare("root", invocation, () => "unused", [], "skill"), message);
  assert.notEqual(queue.prepare("root", invocation, () => "plain-id"), message);
  t.mock.method(globalThis, "fetch", async (_url: string, init: RequestInit) => {
    const body = JSON.parse(String(init.body));
    assert.deepEqual(body, { session_id: "root", prompt: invocation, message_id: "skill-id", command: "skill" });
    return Response.json({ status: "queued", session_id: "root", message_id: "skill-id" });
  });
  await queueMessage("token", message);
});

function browser() {
  const dom = new JSDOM("<button id='trigger'>Skills</button><div id='root'></div>", { url: "http://localhost" });
  Object.assign(dom.window.HTMLElement.prototype, { attachEvent() {}, detachEvent() {} });
  const descriptors = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true, ResizeObserver: class { observe() {} disconnect() {} } });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute("open", ""); };
  dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute("open"); };
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  return { dom, container, root, async cleanup() {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT", "ResizeObserver"]) {
      if (descriptors[key]) Object.defineProperty(globalThis, key, descriptors[key]); else Reflect.deleteProperty(globalThis, key);
    }
  } };
}

test("slash skill rows expose descriptions and keyboard completion while errors can be dismissed", async () => {
  const view = browser(); let prompt = "/rev", submitted = 0;
  const props: ComponentProps<typeof Composer> = { inputRef: null, prompt, setPrompt: value => { prompt = value; }, demo: false,
    disabled: false, canSubmit: true, running: true, submit: () => { submitted++; }, cancel: () => {}, skills: items };
  const render = (extra: Partial<typeof props> = {}) => act(async () => view.root.render(createElement(Composer, { ...props, prompt, ...extra })));
  const key = (key: string) => act(async () => { const input = view.container.querySelector("textarea")!; input.focus(); input.dispatchEvent(new view.dom.window.KeyboardEvent("keydown", { key, bubbles: true, cancelable: true })); });
  try {
    await render();
    assert.match(view.container.querySelector('[role="option"]')!.textContent!, /\/reviewSkillReview a workspace/);
    await key("Tab"); assert.equal(prompt, "/review "); assert.equal(submitted, 0);
    await render(); await key("Enter"); assert.equal(submitted, 1, "Skills remain sendable while another response is running");
    prompt = "/unknown"; await render({ skills: [], skillsError: "Could not load skills" });
    assert.ok(view.container.querySelector(".slash-catalog-status"));
    await key("Escape"); assert.equal(view.container.querySelector(".slash-menu"), null);
  } finally { await view.cleanup(); }
});

test("skill catalog scopes abort old requests and suppress stale credentials, roots and agents", async t => {
  const view = browser(); let current!: SkillsState;
  const pending: { signal: AbortSignal; resolve: (response: Response) => void; url: string; token: string }[] = [];
  t.mock.method(globalThis, "fetch", (url: string, init: RequestInit) => new Promise<Response>(resolve => {
    assert.equal(init.method, "GET"); assert.equal(init.mode, "same-origin");
    pending.push({ signal: init.signal as AbortSignal, resolve, url, token: (init.headers as Record<string, string>)["X-Ngn-Token"] });
  }));
  function Harness({ token, root, agent }: { token: string; root: string; agent: string }) {
    current = useSkills(token, root, agent, "", ""); return createElement("p", null, current.items.map(skill => skill.name).join(","));
  }
  try {
    await act(async () => view.root.render(createElement(Harness, { token: "old", root: "root-a", agent: "assistant" })));
    await act(async () => view.root.render(createElement(Harness, { token: "new", root: "root-b", agent: "reviewer" })));
    assert.equal(pending[0].signal.aborted, true); assert.equal(current.items.length, 0);
    assert.equal(pending[1].url, "/api/skills?session_id=root-b"); assert.equal(pending[1].token, "new");
    await act(async () => pending[0].resolve(Response.json({ session_id: "root-a", agent: "assistant", enabled: true, skills: [skill("old")] })));
    assert.equal(current.items.length, 0);
    await act(async () => pending[1].resolve(Response.json({ session_id: "root-b", agent: "reviewer", enabled: true, skills: [skill("current")] })));
    assert.deepEqual(current.items.map(skill => skill.name), ["current"]);
    await act(async () => view.root.render(createElement(Harness, { token: "rotated", root: "root-b", agent: "reviewer" })));
    assert.equal(current.items.length, 0); assert.equal(pending.length, 3);
    await act(async () => view.root.render(null)); assert.equal(pending[2].signal.aborted, true);
  } finally { await view.cleanup(); }
});

test("skill browsing refreshes on focus, picker opening and run completion with honest failure and disabled states", async t => {
  const view = browser(); let current!: SkillsState, calls = 0, fails = false, enabled = true;
  t.mock.method(globalThis, "fetch", async () => {
    calls++;
    if (fails) throw new Error("Unavailable");
    return Response.json({ session_id: "root", agent: "assistant", enabled, skills: [skill(`revision-${calls}`)] });
  });
  function Harness({ prompt = "", run = "" }: { prompt?: string; run?: string }) { current = useSkills("token", "root", "assistant", prompt, run); return null; }
  try {
    await act(async () => view.root.render(createElement(Harness))); assert.equal(calls, 1);
    await act(async () => view.root.render(createElement(Harness, { prompt: "/" }))); assert.equal(calls, 2);
    await act(async () => view.dom.window.dispatchEvent(new view.dom.window.Event("focus"))); assert.equal(calls, 3);
    await act(async () => view.root.render(createElement(Harness, { prompt: "/", run: "running" })));
    await act(async () => view.root.render(createElement(Harness, { prompt: "/", run: "" }))); assert.equal(calls, 5);
    fails = true; await act(async () => current.refresh()); assert.match(current.error, /Could not load skills/); assert.deepEqual(current.items, []);
    fails = false; enabled = false; await act(async () => current.refresh());
    assert.equal(current.catalog?.enabled, false); assert.deepEqual(current.items, []); assert.equal(current.error, "");
  } finally { await view.cleanup(); }
});

test("skills dialog filters descriptions, selects canonical commands and restores focus", async t => {
  const view = browser(); let current!: SkillsState, chosen = "";
  t.mock.method(globalThis, "fetch", async () => Response.json({ session_id: "root", agent: "assistant", enabled: true, skills: items }));
  function Harness() {
    current = useSkills("token", "root", "assistant", "", "");
    return current.open ? createElement(SkillsDialog, { skills: current, choose: skill => { chosen = skillCommand(skill); current.close(); } }) : null;
  }
  try {
    await act(async () => view.root.render(createElement(Harness)));
    view.dom.window.document.getElementById("trigger")!.focus();
    await act(async () => current.show("sharing"));
    assert.equal(view.dom.window.document.activeElement?.getAttribute("type"), "search");
    assert.equal(view.container.querySelectorAll(".skills-list button").length, 1);
    assert.match(view.container.querySelector(".skills-list")!.textContent!, /\/skill:help/);
    await act(async () => view.container.querySelector<HTMLButtonElement>(".skills-list button")!.click());
    assert.equal(chosen, "skill:help"); assert.equal(view.container.querySelector("dialog"), null);
    assert.equal(view.dom.window.document.activeElement?.id, "trigger");
  } finally { await view.cleanup(); }
});
