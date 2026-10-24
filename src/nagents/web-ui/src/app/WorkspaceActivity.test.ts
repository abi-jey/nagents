import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { WorkspaceActivity } from "./WorkspaceActivity.js";
import { Composer } from "../features/chat/Composer.js";
import { TaskConversation } from "../features/team/TaskConversation.js";
import { taskAgent, type TeamAgent } from "../features/team/model.js";
import type { TeamController } from "../features/team/useTeam.js";
import type { RetainedTask } from "../types.js";

test("workspace polling and recipient switches retain exactly one main conversation and composer", async () => {
  const dom = new JSDOM("<div id='root'></div>", { pretendToBeVisual: true });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
    requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window), cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
    matchMedia: () => ({ matches: false, addEventListener() {}, removeEventListener() {} }),
    ResizeObserver: class { observe() {} disconnect() {} },
  };
  Object.assign(globalThis, globals);
  const container = dom.window.document.getElementById("root")!;
  const react = createRoot(container);
  const child: RetainedTask = { id: "child", name: "Explorer", status: "running", result: "", error: "", session_id: "root",
    prompt: "Inspect repository", child_session_id: "root-child", parent_task_id: "", parent_session_id: "root", depth: 1,
    profile: "research", mode: "build", followups: 0, activation: 0, trigger: "delegation", can_message: true };
  const mainAgent: TeamAgent = { ...taskAgent({ ...child, id: "", name: "Assistant" }), phase: "ready" };
  const agents = [mainAgent, taskAgent(child)];
  const base: TeamController = { selected: "", select() {}, tasks: [child], profiles: [], subagentUsage: undefined, task: undefined, profile: undefined,
    detail: undefined, loaded: true, activity: [], loading: false, error: "", startNotice: "", startParent: undefined,
    startParentId: "", startUnavailable: "", refresh() {}, dismissError() {},
    prompt: "", setPrompt() {}, canMessage: true, submitting: false, async submit() {}, async loadEarlier() {}, loadingEarlier: false };
  const detail = { task: child, history: [{ role: "assistant", content: "Child evidence", name: "", tool_call_id: "", tool_calls: [] }], activity_cursor: 0 };
  const composer = createElement("footer", { className: "composer-area" }, createElement(Composer, { inputRef: null,
    prompt: "Draft retained", setPrompt() {}, demo: false, disabled: false, canSubmit: false, running: false, submit() {}, cancel() {} }));
  async function render(poll: number, selected = "", sessionId = "root") {
    const team: TeamController = { ...base, selected, task: selected ? child : undefined, detail: selected ? detail : undefined,
      profiles: [{ id: "writer", name: "Writer", description: `Catalog poll ${poll}`, model: "test", provider: "openai", used: false, available: true }] };
    await act(async () => react.render(createElement("main", {}, createElement(WorkspaceActivity, {
      sessionId,
      conversation: { entries: [{ id: "main-message", kind: "assistant", text: `Main message ${poll}` }], sessionId, active: !selected, demo: false, canSubmit: false, submit() {} },
      task: selected ? createElement(TaskConversation, { team, agents, arrivals: {}, token: "", sessionId, back() {} }) : undefined,
      composer,
      team: { team, agents, arrivals: {}, open: true, close() {}, configure() {} },
    }))));
  }
  try {
    await render(0);
    const original = container.querySelector(".conversation")!;
    for (let poll = 1; poll <= 32; poll++) {
      const selected = poll % 3 === 0 ? "child" : "";
      await render(poll, selected);
      assert.equal(container.querySelectorAll(".conversation").length, selected ? 2 : 1, `No orphaned conversation at poll ${poll}`);
      assert.equal(container.querySelectorAll(".conversation:not([hidden])").length, 1);
      assert.equal(container.querySelectorAll(".team-pane").length, 1);
      assert.equal(container.querySelectorAll("textarea").length, 1);
      assert.ok(original.isConnected, "Main reading surface preserves its DOM identity while inspecting children");
      assert.equal(original.querySelector(".entry-content")?.textContent, `Main message ${poll}`);
    }
    await render(33, "", "another-root");
    assert.equal(original.isConnected, false, "A different root replaces rather than retaining the old reading surface");
    assert.equal(container.querySelectorAll(".conversation").length, 1);
  } finally {
    await act(async () => react.unmount()); dom.window.close();
    for (const name of Object.keys(globals)) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});
