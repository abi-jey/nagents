import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import type { ArtifactImageRef, RetainedTask, Snapshot, WireEvent } from "../../types.js";
import { defaultAppearance } from "../settings/appearance.js";
import { taskEntries, TaskTranscript } from "../team/TaskConversation.js";
import type { TaskActivity, TaskDetail, TeamAgent } from "../team/model.js";
import { AGENT_UPDATES_PREFIX, agentUpdates } from "./backgroundUpdates.js";
import { Conversation } from "./Conversation.js";
import { DelegationFlow } from "./delegationFlow.js";
import { appendEvent, fromHistory, type Entry } from "./transcript.js";
import { applySnapshot } from "./liveTranscript.js";
import { artifactState } from "./artifactImages.js";

const image = (history = "8", index = 0, values: Partial<ArtifactImageRef> = {}): ArtifactImageRef => ({
  type: "artifact_image", artifact_id: "thumbnail", artifact_part: 0, filename: "thumbnail.png", media_type: "image/png",
  byte_length: 3, path: `/api/sessions/root/artifacts/${history}/${index}`, caption: "A returned thumbnail.", ...values,
});
const metadata = (status = "completed") => ({ response_id: "response", response_session_id: "root", run_id: "run",
  revision: status === "completed" ? 2 : 1, round_number: 0, history_id: "8", status, summary: "",
  summary_truncated: false, summary_expired: false, usage_reported: false });
const event = (artifacts: unknown, values: Partial<WireEvent> = {}): WireEvent => ({
  event: "remote_artifacts", run_id: "run", response_id: "response", response_session_id: "root", history_id: "8", artifacts, ...values,
});
const saved = (artifacts: ArtifactImageRef[], text = ""): Snapshot => ({ session_id: "root", sessions: [], retained_tasks: [], history: [
  { role: "assistant", content: text, name: "", tool_call_id: "", tool_calls: [], history_id: "8",
    response_id: "response", response_metadata: metadata(), parts: artifacts },
] });
const task = (id = "child"): RetainedTask => ({ session_id: id, root_session_id: "root", parent_session_id: "",
  title: id, status: "running", result: "", error: "", prompt: "Return a thumbnail.", profile: "remote", mode: "build",
  depth: 1, followups: 0, activation: 0, trigger: "delegation" });
const agent = (id = "child"): TeamAgent => ({ id, name: id, profile: "remote", parentId: "", prompt: "Return a thumbnail.",
  phase: "running", reason: "", provider: "", providerKind: "a2a", model: "", startedAt: 1, updatedAt: 1,
  canMessage: true, messageReason: "" });
const markup = (entries: Entry[]) => new JSDOM(renderToStaticMarkup(createElement(Conversation, {
  entries, sessionId: "root", demo: false, canSubmit: false, submit() {}, appearance: defaultAppearance,
})));

function browser(t: TestContext) {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true, IntersectionObserver: undefined });
  Object.assign(dom.window.HTMLDialogElement.prototype, {
    showModal(this: HTMLDialogElement) { this.setAttribute("open", ""); },
    close(this: HTMLDialogElement) { this.removeAttribute("open"); },
  });
  const fetched = t.mock.method(globalThis, "fetch", async () => new Response(new Uint8Array([1, 2, 3]), { headers: { "Content-Type": "image/png" } }));
  t.mock.method(URL, "createObjectURL", () => "blob:artifact-image");
  t.mock.method(URL, "revokeObjectURL", () => {});
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  return { dom, container, fetched,
    render: (entries: Entry[]) => act(async () => root.render(createElement(Conversation, {
      entries, sessionId: "root", token: "private-token", demo: false, canSubmit: false, submit() {}, appearance: defaultAppearance,
    }))),
    async close() {
      await act(async () => root.unmount()); dom.window.close();
      for (const key of ["window", "document", "HTMLElement", "IS_REACT_ACT_ENVIRONMENT", "IntersectionObserver"])
        if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
    } };
}

test("durable image-only metadata creates one visible response and survives empty final text and history", () => {
  const ref = image();
  let entries = appendEvent([], { event: "text_done", run_id: "run", response_id: "response", text: "" });
  assert.deepEqual(entries, [], "empty text alone never creates a ghost response");
  entries = appendEvent(entries, event([ref]));
  assert.equal(entries.length, 1);
  const id = entries[0].id;
  entries = appendEvent(entries, { ...metadata(), event: "response_metadata" });
  entries = appendEvent(entries, { event: "text_done", run_id: "run", response_id: "response", response_session_id: "root", text: "" });
  entries = appendEvent(entries, event([ref, ref]));
  assert.deepEqual(entries[0].artifacts, [ref]);
  assert.equal(entries[0].id, id);
  const refreshed = applySnapshot({ entries }, saved([ref]));
  assert.equal(refreshed.entries.length, 1);
  assert.equal(refreshed.entries[0].id, id);
  assert.deepEqual(refreshed.entries[0].artifacts, [ref]);
  const cold = markup(fromHistory(saved([ref])));
  assert.equal(cold.window.document.querySelectorAll(".chat-media-image").length, 1);
  cold.window.close();
});

test("late artifact metadata enriches a final response without reaccepting stale text or completing live work", () => {
  let entries = appendEvent([], { event: "text_chunk", run_id: "run", response_id: "response", response_session_id: "root", chunk: "Caption" });
  const id = entries[0].id;
  entries = appendEvent(entries, event([image()]));
  assert.equal(entries[0].streaming, true, "attachment metadata has no completion authority");
  entries = appendEvent(entries, { ...metadata(), event: "response_metadata" });
  entries = appendEvent(entries, { event: "text_done", run_id: "run", response_id: "response", text: "Final caption" });
  entries = appendEvent(entries, event([image()]));
  entries = appendEvent(entries, { event: "text_chunk", run_id: "run", response_id: "response", chunk: " stale replay" });
  assert.equal(entries.length, 1);
  assert.equal(entries[0].id, id);
  assert.equal(entries[0].text, "Final caption");
  assert.equal(entries[0].streaming, false);
  assert.deepEqual(entries[0].artifacts, [image()]);
});

test("artifact identity separates source sessions, PNG/JPEG parts and repeated protocol metadata", () => {
  const first = image(), second = image("8", 1, { artifact_part: 1, media_type: "image/jpeg", filename: "thumbnail.jpg" });
  let entries = appendEvent([], event([first, second]));
  entries = appendEvent(entries, event([first, second]));
  assert.equal(entries.length, 1);
  assert.deepEqual(entries[0].artifacts, [first, second]);
  entries = appendEvent(entries, event([image("9")], { response_session_id: "other", session_id: "other", history_id: "9" }));
  assert.equal(entries.length, 2);
  assert.equal(entries[0].responseSessionId, "root");
  assert.equal(entries[1].responseSessionId, "other");
  assert.deepEqual(entries[0].artifacts, [first, second]);
  const normalized = artifactState([{ ...first, data_base64: "must-not-enter-the-ui-model", url: "https://remote.invalid/image.png" }]);
  assert.deepEqual(normalized.artifacts, [first], "only bounded local reference metadata enters the view model");
});

test("artifact cards keep their DOM through hydration and reuse authenticated download and image viewer", async t => {
  const ui = browser(t), ref = image();
  try {
    let entries = appendEvent([], event([ref]));
    await ui.render(entries);
    const card = ui.container.querySelector(".chat-media-image")!;
    assert.ok(card);
    assert.equal(card.closest("[hidden]"), null);
    assert.equal(ui.fetched.mock.callCount(), 1);
    const request = ui.fetched.mock.calls[0].arguments;
    assert.equal(request[0], ref.path);
    assert.equal(request[1]?.mode, "same-origin");
    assert.equal(request[1]?.credentials, "same-origin");
    assert.equal(request[1]?.redirect, "error");
    assert.deepEqual(request[1]?.headers, { "X-Ngn-Token": "private-token" });
    entries = applySnapshot({ entries }, saved([ref])).entries;
    await ui.render(entries);
    assert.equal(ui.container.querySelector(".chat-media-image"), card);
    assert.equal(ui.container.querySelectorAll(".chat-media-image").length, 1);
    assert.equal(ui.fetched.mock.callCount(), 1);
    const download = card.querySelector<HTMLAnchorElement>("a[download]")!;
    assert.equal(download.href, "blob:artifact-image");
    assert.equal(download.download, "thumbnail.png");
    await act(async () => card.querySelector<HTMLButtonElement>(".chat-image-open")!.click());
    assert.equal(ui.container.querySelector("dialog img")?.getAttribute("src"), "blob:artifact-image");
    assert.equal(ui.fetched.mock.callCount(), 1);
  } finally { await ui.close(); }
});

test("invalid artifact routes, types and budgets show an explicit error and never fetch", async t => {
  const ui = browser(t), ref = image();
  const invalid: unknown[] = [
    [{ ...ref, path: "https://remote.invalid/image.png" }], [{ ...ref, path: "//remote.invalid/a" }],
    [{ ...ref, path: "/api/sessions/root/artifacts/0/0" }], [{ ...ref, path: "/api/sessions/root/artifacts/8/256" }],
    [{ ...ref, path: "/api/sessions/%2e%2e/artifacts/8/0" }], [{ ...ref, path: `${ref.path}?token=secret` }],
    [{ ...ref, path: `${ref.path}#image` }], [{ ...ref, media_type: "image/svg+xml" }],
    [{ ...ref, byte_length: 0 }], [{ ...ref, byte_length: 8 * 1024 * 1024 + 1 }],
    [ref, image("9"), image("10"), image("11")],
    [image("8", 0, { byte_length: 5 * 1024 * 1024 }), image("8", 1, { byte_length: 5 * 1024 * 1024 })],
  ];
  try {
    for (const values of invalid) {
      const entries = appendEvent([], event(values));
      await ui.render(entries);
      assert.match(ui.container.querySelector('[role="status"].artifact-image-error')?.textContent || "", /image|artifact/i);
      assert.equal(ui.container.querySelector(".chat-media-image"), null);
    }
    assert.equal(ui.fetched.mock.callCount(), 0);
  } finally { await ui.close(); }
});

test("selected child activity and saved image-only history keep one attachment and reject sibling identity", () => {
  const ref = image(), detail: TaskDetail = { task: task(), history: [], activity_cursor: 0 };
  const activity = (sequence: number, session: string, payload: TaskActivity["payload"]): TaskActivity => ({
    event: "task_activity", root_session_id: "root", parent_session_id: "", session_id: session, activation: 0,
    sequence, timestamp: sequence, payload,
  });
  const records = [activity(1, "child", { type: "remote_artifacts", response_id: "child-response", response_session_id: "child", history_id: "8", artifacts: [ref] }),
    activity(2, "child", { type: "remote_artifacts", response_id: "forged", response_session_id: "sibling", history_id: "9", artifacts: [image("9")] }),
    activity(3, "sibling", { type: "remote_artifacts", response_id: "sibling", response_session_id: "sibling", history_id: "9", artifacts: [image("9")] })];
  const transcript = new TaskTranscript();
  const first = transcript.update(detail, records);
  assert.equal(first.length, 1);
  assert.deepEqual(first[0].artifacts, [ref]);
  const history = saved([ref]).history.map(row => ({ ...row, response_id: "child-response", response_metadata: { ...metadata(), response_id: "child-response", response_session_id: "child" } }));
  const second = transcript.update({ ...detail, history }, records);
  assert.equal(second.length, 1);
  assert.equal(second[0].id, first[0].id);
  assert.deepEqual(second[0].artifacts, [ref]);
  assert.deepEqual(taskEntries({ ...detail, history }, records)[0].artifacts, [ref]);
});

test("only verified lifecycle completion exposes one canonical delegation image while background copies fold", () => {
  const ref = image(), copy = image("12"), flow = new DelegationFlow();
  let entries = appendEvent([], { event: "tool_call", run_id: "run", id: "delegate", name: "to_agent", arguments: { agent: "remote", prompt: "Return a thumbnail." } });
  entries = appendEvent(entries, { event: "tool_result", run_id: "run", id: "delegate", name: "to_agent", result: { session_id: "child" } });
  entries = appendEvent(entries, { event: "task_started", run_id: "run", session_id: "child", parent_session_id: "", activation: 0 });
  const activity: TaskActivity[] = [{ event: "task_activity", root_session_id: "root", parent_session_id: "", session_id: "child", activation: 0,
    sequence: 1, timestamp: 1, payload: { type: "remote_artifacts", response_id: "child-response", response_session_id: "child", history_id: "8", artifacts: [ref] } }];
  const options = { agents: [agent()], activity, inspect() {} };
  const pending = markup(flow.project(entries, options));
  assert.equal(pending.window.document.querySelectorAll(".chat-media-image").length, 0);
  pending.window.close();
  entries = appendEvent(entries, { event: "task_completed", run_id: "run", session_id: "child", parent_session_id: "", activation: 0, status: "completed", result: "", artifacts: [ref] });
  const context = AGENT_UPDATES_PREFIX + JSON.stringify({ sessions: [{ session_id: "child", status: "completed", result: "", artifacts: [copy] }], human_messages: [] });
  assert.ok(agentUpdates(context), "artifact metadata must not break completion-context recognition");
  entries.push({ id: "context-copy", kind: "context", text: context, artifacts: [copy] });
  const projected = flow.project(entries, options), complete = markup(projected);
  assert.equal(projected.some(entry => entry.id === "context-copy"), false);
  assert.equal(complete.window.document.querySelectorAll(".chat-media-image").length, 1);
  assert.equal(complete.window.document.querySelector(".chat-media-image")?.closest("[hidden]"), null);
  const result = projected.find(entry => entry.delegation?.kind === "result")!;
  assert.deepEqual(result.delegation && "artifacts" in result.delegation ? result.delegation.artifacts : undefined, [ref]);
  complete.window.close();
});

test("cold delegation history preserves images for distinct tasks sharing an artifact ID", () => {
  const refs = [image("8"), image("9")];
  const snapshot: Snapshot = { session_id: "root", sessions: [], retained_tasks: [
    { ...task("one"), status: "completed", artifacts: [refs[0]] }, { ...task("two"), status: "completed", artifacts: [refs[1]] },
  ], history: [{ role: "assistant", content: "", name: "", history_id: "2", tool_call_id: "", tool_calls: [
    { id: "one-call", name: "to_agent", arguments: { agent: "remote", prompt: "One image" } },
    { id: "two-call", name: "to_agent", arguments: { agent: "remote", prompt: "Another image" } },
  ] }, ...["one", "two"].map((id, index) => ({ role: "tool", content: JSON.stringify({ session_id: id }), name: "to_agent",
    tool_call_id: `${id}-call`, history_id: String(3 + index), tool_calls: [] }))] };
  const entries = fromHistory(snapshot), flow = new DelegationFlow();
  const projected = flow.project(entries, { agents: [agent("one"), agent("two")], activity: [], inspect() {} });
  const results = projected.filter(entry => entry.delegation?.kind === "result");
  assert.equal(results.length, 2);
  const page = markup(projected);
  assert.equal(page.window.document.querySelectorAll(".chat-media-image").length, 2);
  for (const ref of refs) assert.ok(results.some(entry => entry.delegation && "artifacts" in entry.delegation &&
    Array.isArray(entry.delegation.artifacts) && entry.delegation.artifacts.some(part => part.path === ref.path)));
  page.window.close();
});
