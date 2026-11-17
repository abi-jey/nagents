import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import test from "node:test";

class Element {
  constructor(tag = "div") { this.tagName = tag; this.children = []; this._text = ""; this.value = ""; this.hidden = false; this.disabled = false; this.scrollTop = 0; this.clientHeight = 800; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set innerHTML(_) { throw new Error("Untrusted content must never be assigned as HTML"); }
  append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
  replaceChildren(...children) { this._text = ""; this.children = []; this.append(...children); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((child) => child !== this); }
  get firstChild() { return this.children[0]; }
  get scrollHeight() { return this.children.length * 50; }
  focus() {}
}

const response = (body, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) });
const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() { let resolve; const promise = new Promise((answer) => { resolve = answer; }); return { promise, resolve }; }
let bootCount = 0;

async function boot() {
  const elements = new Map();
  const requests = [];
  const sockets = [];
  const states = [];
  const submissions = [];
  const tasks = [];
  const queues = [];
  const session = { id: "ngn-test", title: "Test conversation" };
  const queue = { session_id: session.id, paused: false, entries: [] };
  globalThis.document = {
    getElementById(id) { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); },
    createElement(tag) { return new Element(tag); },
    createTextNode(text) { const node = new Element("text"); node.textContent = text; return node; },
  };
  globalThis.location = { protocol: "http:", host: "127.0.0.1:8765" };
  Object.defineProperty(globalThis, "crypto", { value: webcrypto, configurable: true });
  globalThis.WebSocket = class {
    constructor(url, protocols) { this.url = url; this.protocols = protocols; sockets.push(this); }
    close() { this.onclose?.(); }
  };
  globalThis.fetch = async (path, options) => {
    requests.push({ path, options });
    if (path === "/api/bootstrap") return response({ token: "host-token", workspace: "/workspace", backend: "demo", read_only: false });
    if (path === "/api/sessions") return response({ sessions: [session] });
    if (path === "/api/state") return states.length ? states.shift() : response({ runs: [], approvals: [] });
    if (path.endsWith("/tasks")) return response({ tasks, limits: { max_depth: 2, max_concurrent: 3, max_executions: 8 } });
    if (path.endsWith("/queue")) {
      if (options.method === "POST") { const { action } = JSON.parse(options.body); queue.paused = action !== "resume"; if (action === "clear") queue.entries.length = 0; }
      return queues.length ? queues.shift() : response({ queue, limit: 32 });
    }
    if (path.endsWith("/cancel") || path === "/api/approvals") return response({ accepted: true });
    if (path.endsWith("/runs")) return submissions.length ? submissions.shift() : response({ run: { id: "run-test" } });
    throw new Error(`Unexpected request: ${path}`);
  };
  await import(`./app.js?test=${++bootCount}`);
  const emit = (sequence, event, data = {}) => sockets[0].onmessage({ data: JSON.stringify({ schema_version: 1, session_id: session.id, run_id: "run-test", sequence, event, ...data }) });
  return { elements, requests, sockets, states, submissions, tasks, queues, queue, session, emit };
}

test("browser client uses host schema and does not duplicate completed or replayed text", async () => {
  const app = await boot();
  assert.deepEqual(app.sockets[0].protocols, ["ngn.events.v1", "ngn.token.host-token"]);
  assert(!app.sockets[0].url.includes("host-token"));
  app.emit(1, "run_started", { prompt: "hello" });
  app.emit(2, "text_delta", { text: "Hello" });
  app.emit(3, "text_completed", { text: "Hello" });
  app.emit(3, "text_completed", { text: "Hello" });
  const assistants = app.elements.get("transcript").children.filter((node) => node.className === "message assistant");
  assert.equal(assistants.length, 1);
  assert.equal(assistants[0].children[1].textContent, "Hello");
  app.emit(4, "text_completed", { text: '<img src=x onerror="steal()">' });
  assert.equal(app.elements.get("transcript").children.at(-1).children[1].textContent, '<img src=x onerror="steal()">');
  await flush();
});

test("stale state responses cannot overwrite newer finished state", async () => {
  const app = await boot();
  const older = deferred(), newer = deferred();
  app.states.push(older.promise, newer.promise);
  app.emit(1, "run_started", { prompt: "hello" });
  app.emit(2, "run_finished", { status: "completed", error: "" });
  newer.resolve(response({ runs: [], approvals: [] }));
  await flush();
  older.resolve(response({ runs: [{ id: "run-test", session_id: "ngn-test" }], approvals: [] }));
  await flush();
  assert.equal(app.elements.get("send").disabled, false);
  assert.equal(app.elements.get("cancel").hidden, true);
});

test("concurrent send gestures produce one idempotent submission", async () => {
  const app = await boot();
  const accepted = deferred();
  app.submissions.push(accepted.promise);
  app.elements.get("prompt").value = "inspect files";
  const first = app.elements.get("composer").onsubmit({ preventDefault() {} });
  const second = app.elements.get("composer").onsubmit({ preventDefault() {} });
  assert.equal(app.requests.filter((request) => request.path.endsWith("/runs")).length, 1);
  accepted.resolve(response({ run: { id: "run-test" } }));
  await Promise.all([first, second]);
});

test("failed submissions retain input and reuse their request ID on retry", async () => {
  const app = await boot();
  app.submissions.push(response({ detail: "temporarily unavailable" }, 503));
  app.elements.get("prompt").value = "inspect files";
  await app.elements.get("composer").onsubmit({ preventDefault() {} });
  assert.equal(app.elements.get("prompt").value, "inspect files");
  await app.elements.get("composer").onsubmit({ preventDefault() {} });
  const submissions = app.requests.filter((request) => request.path.endsWith("/runs"));
  assert.equal(submissions.length, 2);
  assert.equal(JSON.parse(submissions[0].options.body).request_id, JSON.parse(submissions[1].options.body).request_id);
});

test("replayed approval events do not revive an expired decision", async () => {
  const app = await boot();
  app.emit(1, "approval_requested", { approval_id: "expired", call: { name: "shell" }, preview: "old" });
  await flush();
  assert.equal(app.elements.get("approval").hidden, true);
});

test("subagent inspection and cancellation use child identity without contaminating the parent transcript", async () => {
  const app = await boot();
  const task = { run_id: "run-child", session_id: "ngn-child", parent_run_id: "run-test", parent_session_id: "ngn-test", root_session_id: "ngn-test", title: "Reviewer", depth: 1, read_only: true, status: "running" };
  app.tasks.push(task);
  app.emit(1, "task_started", { task });
  await flush();
  assert.equal(app.elements.get("tasks-panel").hidden, false);
  assert(app.elements.get("tasks").textContent.includes("Reviewer"));
  app.elements.get("tasks").children[0].children[1].onclick();
  assert.equal(app.sockets.length, 2);
  assert(app.sockets[1].url.includes("session_id=ngn-child"));
  app.sockets[1].onmessage({ data: JSON.stringify({ session_id: "ngn-child", run_id: "run-child", sequence: 2, event: "text_completed", text: "Child-only response" }) });
  assert(app.elements.get("task-transcript").textContent.includes("Child-only response"));
  assert(!app.elements.get("transcript").textContent.includes("Child-only response"));
  app.elements.get("tasks").children[0].children[1].onclick();
  app.sockets[1].onmessage({ data: JSON.stringify({ session_id: "ngn-child", run_id: "run-child", sequence: 3, event: "text_completed", text: "Stale inspector response" }) });
  assert(!app.elements.get("task-transcript").textContent.includes("Stale inspector response"));
  app.elements.get("cancel-task").onclick();
  await flush();
  assert(app.requests.some((request) => request.path === "/api/runs/run-child/cancel"));
  app.elements.get("close-task").onclick();
  assert.equal(app.elements.get("task-inspector").hidden, true);
});

test("a root operator can approve a child call, bound to the actual child run", async () => {
  const app = await boot();
  app.states.push(response({ runs: [{ id: "run-test", session_id: "ngn-test" }], approvals: [{ id: "approval-child", run_id: "run-child", session_id: "ngn-child", root_session_id: "ngn-test", agent_title: "Writer", depth: 1, call: { name: "write" }, preview: "exact child change" }] }));
  app.emit(1, "task_updated", { task: { run_id: "run-child" } });
  await flush();
  assert.equal(app.elements.get("approval").hidden, false);
  assert(app.elements.get("approval-label").textContent.includes("Writer"));
  await app.elements.get("allow").onclick();
  const decision = app.requests.find((request) => request.path === "/api/approvals");
  assert.deepEqual(JSON.parse(decision.options.body), { run_id: "run-child", approval_id: "approval-child", allow: true });
});

test("busy conversations accept queued inputs and pending controls target exact identities", async () => {
  const app = await boot();
  app.states.push(response({ runs: [{ id: "run-active", session_id: "ngn-test" }], approvals: [] }));
  app.queue.entries.push({ run: { id: "run-pending", session_id: "ngn-test" }, prompt: '<script>untrusted()</script>' });
  app.emit(1, "run_started", { prompt: "busy" });
  await flush();
  assert.equal(app.elements.get("send").disabled, false);
  assert.equal(app.elements.get("send").textContent, "Queue ↗");
  assert(app.elements.get("queue").textContent.includes('<script>untrusted()</script>'));
  app.elements.get("prompt").value = "next input";
  await app.elements.get("composer").onsubmit({ preventDefault() {} });
  assert(app.requests.some((request) => request.path.endsWith("/runs") && JSON.parse(request.options.body).prompt === "next input"));
  await app.elements.get("queue").children[0].children[1].onclick();
  assert(app.requests.some((request) => request.path === "/api/runs/run-pending/cancel"));
  assert(!app.requests.some((request) => request.path === "/api/runs/run-active/cancel"));
  await app.elements.get("pause-queue").onclick();
  assert.equal(app.elements.get("pause-queue").textContent, "Resume queue");
  await app.elements.get("pause-queue").onclick();
  await app.elements.get("clear-queue").onclick();
  const actions = app.requests.filter((request) => request.path.endsWith("/queue") && request.options.method === "POST").map((request) => JSON.parse(request.options.body).action);
  assert.deepEqual(actions, ["pause", "resume", "clear"]);
  assert(app.elements.get("queue-status").textContent.includes("Paused"));
});

test("stale queue snapshots cannot replace newer state and replay never duplicates user turns", async () => {
  const app = await boot();
  const older = deferred(), newer = deferred();
  app.queues.push(older.promise, newer.promise);
  app.emit(1, "run_queued", { prompt: "pending" });
  app.emit(2, "queue_updated", { paused: true, run_id: "" });
  newer.resolve(response({ queue: { session_id: "ngn-test", paused: true, entries: [] } }));
  await flush();
  older.resolve(response({ queue: { session_id: "ngn-test", paused: false, entries: [{ run: { id: "old" }, prompt: "stale" }] } }));
  await flush();
  assert(app.elements.get("queue-status").textContent.includes("Paused"));
  assert.equal(app.elements.get("queue").children.length, 0);
  app.emit(3, "run_started", { prompt: "pending" });
  app.emit(3, "run_started", { prompt: "pending" });
  assert.equal(app.elements.get("transcript").children.filter((child) => child.className === "message user").length, 1);
  await flush();
});

test("a delayed accepted submission does not erase a newer draft", async () => {
  const app = await boot();
  const accepted = deferred();
  app.submissions.push(accepted.promise);
  app.elements.get("prompt").value = "first input";
  const sending = app.elements.get("composer").onsubmit({ preventDefault() {} });
  app.elements.get("prompt").value = "newer draft";
  accepted.resolve(response({ run: { id: "accepted", status: "queued" } }));
  await sending;
  assert.equal(app.elements.get("prompt").value, "newer draft");
});

test("accepted inputs synchronize the conversation heading with the host title", async () => {
  const app = await boot();
  app.session.title = "Updated host title";
  app.elements.get("prompt").value = "first input";
  await app.elements.get("composer").onsubmit({ preventDefault() {} });
  assert.equal(app.elements.get("title").textContent, "Updated host title");
  assert(app.elements.get("sessions").textContent.includes("Updated host title"));
});
