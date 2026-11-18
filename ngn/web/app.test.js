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
  requestSubmit() { return this.onsubmit?.({ preventDefault() {} }); }
}

const response = (body, status = 200) => ({ ok: status < 400, status, text: async () => JSON.stringify(body) });
const flush = () => new Promise((resolve) => setImmediate(resolve));
function deferred() { let resolve; const promise = new Promise((answer) => { resolve = answer; }); return { promise, resolve }; }
let bootCount = 0;

async function boot({ touch = false } = {}) {
  const elements = new Map();
  const requests = [];
  const sockets = [];
  const states = [];
  const submissions = [];
  const tasks = [];
  const queues = [];
  const steerings = [], steeringPosts = [], steering = new Map();
  const session = { id: "ngn-test", title: "Test conversation" };
  const catalog = [session];
  const queue = { session_id: session.id, paused: false, entries: [] };
  const pointer = { matches: touch, addEventListener(_kind, handler) { this.changed = handler; } };
  globalThis.matchMedia = () => pointer;
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
    if (path === "/api/bootstrap") return response({ token: "host-token", workspace: "/workspace", backend: "nagents", read_only: false });
    if (path === "/api/sessions") {
      if (options.method === "POST") { const created = { id: `ngn-new-${catalog.length}`, title: "New conversation" }; catalog.unshift(created); return response({ session: created }); }
      return response({ sessions: catalog });
    }
    if (path === "/api/state") return states.length ? states.shift() : response({ runs: [], approvals: [] });
    if (path.endsWith("/steer")) {
      const run = path.split("/").at(-2);
      const state = steering.get(run) || { run_id: run, session_id: session.id, run_status: "running", supported: false, accepting: false, receipts: [] };
      if (options.method === "GET") return steerings.length ? steerings.shift() : response({ steering: state });
      if (steeringPosts.length) return steeringPosts.shift();
      const body = JSON.parse(options.body), receipt = { ...body, input_id: `steer-${state.receipts.length}`, run_id: run, session_id: state.session_id, position: state.receipts.length + 1, status: "pending" };
      state.receipts.push(receipt);
      return response({ receipt });
    }
    if (path.endsWith("/tasks")) return response({ tasks, limits: { max_depth: 2, max_concurrent: 3, max_executions: 8 } });
    if (path.endsWith("/queue")) {
      if (options.method === "POST") { const { action } = JSON.parse(options.body); queue.paused = action !== "resume"; if (action === "clear") queue.entries.length = 0; }
      return queues.length ? queues.shift() : response({ queue: { ...queue, session_id: path.split("/").at(-2) }, limit: 32 });
    }
    if (path.endsWith("/cancel") || path === "/api/approvals") return response({ accepted: true });
    if (path.endsWith("/runs")) return submissions.length ? submissions.shift() : response({ run: { id: "run-test" } });
    throw new Error(`Unexpected request: ${path}`);
  };
  const presentation = await import(`./app.js?test=${++bootCount}`);
  const emit = (sequence, event, data = {}) => sockets[0].onmessage({ data: JSON.stringify({ schema_version: 1, session_id: session.id, run_id: "run-test", sequence, event, ...data }) });
  return { elements, requests, sockets, states, submissions, tasks, queues, queue, session, emit, presentation, pointer, catalog, steerings, steeringPosts, steering };
}

async function enableSteering(app, sequence = 1, run = "run-test") {
  app.steering.set(run, { run_id: run, session_id: app.session.id, run_status: "running", supported: true, accepting: true, receipts: [] });
  app.states.push(response({ runs: [{ id: run, session_id: app.session.id }], approvals: [] }));
  app.emit(sequence, "run_started", { run_id: run, prompt: "active" });
  await flush();
  assert(app.elements.get("steering-status").textContent.includes(run));
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

test("compact durations keep adjacent units and never expand beyond eight cells", async () => {
  const { presentation } = await boot();
  for (const [value, expected] of [[0, "0ms"], [1, "1ms"], [999, "999ms"], [1000, "1s"], [1250, "1.25s"], [9990, "9.99s"], [10000, "10s"], [12340, "12.3s"], [59900, "59.9s"], [59960, "1m"], [60000, "1m"], [61400, "1m 1s"], [61600, "1m 2s"], [3599600, "1h"], [3661000, "1h 1m"], [86399600, "1d"], [90061000, "1d 1h"], [86400000 * 9999 + 3600000 * 23, "9999d"], [86400000 * 10000000, ">999999d"]]) {
    assert.equal(presentation.formatDuration(value), expected);
    assert(presentation.formatDuration(value).length <= 8);
  }
  assert.equal(presentation.formatDuration(NaN), "unknown");
  assert.equal(presentation.formatDuration(Infinity), "unknown");
});

test("native tool summaries retain recognized targets and freeze at host approval snapshots", async () => {
  const app = await boot();
  const call = { call_id: "write-1", name: "write", arguments: { path: "hello.txt", content: "do not show arbitrary content in summary" } };
  app.emit(1, "tool_started", { call });
  const summary = app.elements.get("transcript").children.at(-1).children[0];
  assert.equal(summary.textContent, "write · hello.txt · running");
  app.emit(2, "tool_approval_timing", { call_id: call.call_id, elapsed_ms: 1250, waiting: true });
  assert.equal(summary.textContent, "write · hello.txt · waiting for approval · 1.25s active");
  app.emit(3, "tool_approval_timing", { call_id: call.call_id, elapsed_ms: 1250, waiting: false });
  app.emit(4, "tool_completed", { result: { call_id: call.call_id, name: "write", result: {}, error: "" }, duration_ms: 1500 });
  assert.equal(summary.textContent, "write · hello.txt · done · 1.5s");
  assert.equal(app.presentation.toolTarget({ name: "custom", arguments: { name: "private" } }), "");
  assert.equal(app.presentation.toolTarget({ name: "skill", arguments: { name: "ngn-\ncustomize\0" } }), " · ngn- customize");
  assert.equal(app.presentation.toolTarget({ name: "shell", arguments: { command: "x".repeat(200) } }).length, 100);
});

test("touch Return keeps a draft local and deliberate gestures preserve composer focus", async () => {
  const app = await boot({ touch: true });
  const prompt = app.elements.get("prompt");
  assert.equal(prompt.enterKeyHint, "enter");
  prompt.value = "unsent draft";
  let prevented = false;
  prompt.onkeydown({ key: "Enter", preventDefault() { prevented = true; } });
  assert.equal(prevented, false);
  assert.equal(prompt.value, "unsent draft");
  assert.equal(app.requests.filter((request) => request.path.endsWith("/runs")).length, 0);
  app.elements.get("send").onmousedown({ preventDefault() { prevented = true; } });
  assert.equal(prevented, true);
  prevented = false;
  prompt.onkeydown({ key: "Enter", ctrlKey: true, preventDefault() { prevented = true; } });
  await flush();
  assert.equal(prevented, true);
  assert.equal(app.requests.filter((request) => request.path.endsWith("/runs")).length, 1);
});

test("desktop send, newlines and IME remain distinct when pointer capabilities change", async () => {
  const app = await boot();
  const prompt = app.elements.get("prompt");
  assert.equal(prompt.enterKeyHint, "send");
  prompt.value = "draft";
  prompt.onkeydown({ key: "Enter", shiftKey: true, preventDefault() { assert.fail("Shift+Enter is a newline"); } });
  prompt.onkeydown({ key: "Enter", isComposing: true, preventDefault() { assert.fail("IME completion is not submission"); } });
  assert.equal(app.requests.filter((request) => request.path.endsWith("/runs")).length, 0);
  app.pointer.matches = true;
  app.pointer.changed();
  assert.equal(prompt.enterKeyHint, "enter");
  app.pointer.matches = false;
  app.pointer.changed();
  let prevented = false;
  prompt.onkeydown({ key: "Enter", preventDefault() { prevented = true; } });
  await flush();
  assert.equal(prevented, true);
  assert.equal(app.requests.filter((request) => request.path.endsWith("/runs")).length, 1);
});

test("mobile navigation creates and reopens the same host-owned sessions as desktop", async () => {
  const app = await boot({ touch: true });
  const picker = app.elements.get("mobile-sessions");
  assert.equal(picker.value, app.session.id);
  assert.deepEqual(picker.children.map((option) => option.value), [app.session.id]);
  await app.elements.get("mobile-new-session").onclick();
  assert.equal(app.catalog.length, 2);
  assert.equal(picker.value, app.catalog[0].id);
  assert.equal(app.elements.get("title").textContent, app.catalog[0].title);
  picker.value = app.session.id;
  await picker.onchange();
  assert.equal(app.elements.get("title").textContent, app.session.title);
  assert(app.sockets.at(-1).url.includes(`session_id=${app.session.id}`));
  assert.equal(app.requests.filter((request) => request.path === "/api/sessions" && request.options.method === "POST").length, 1);
});

test("only exact capable accepting roots enable deliberate steering; Send stays a separate FIFO kind", async () => {
  const app = await boot();
  app.elements.get("prompt").value = "retain unsupported draft";
  assert.equal(app.elements.get("steer").disabled, true);
  await app.elements.get("steer").onclick();
  assert.equal(app.requests.filter((request) => request.path.endsWith("/steer") && request.options.method === "POST").length, 0);
  assert.equal(app.elements.get("prompt").value, "retain unsupported draft");
  await enableSteering(app);
  app.elements.get("prompt").value = "steered input";
  await app.elements.get("steer").onclick();
  app.elements.get("prompt").value = "queued input";
  await app.elements.get("composer").requestSubmit();
  const steer = app.requests.find((request) => request.path.endsWith("/steer") && request.options.method === "POST");
  const fifo = app.requests.find((request) => request.path.endsWith("/runs"));
  assert.equal(steer.path, "/api/runs/run-test/steer");
  assert.notEqual(JSON.parse(steer.options.body).request_id, JSON.parse(fifo.options.body).request_id);
  assert.equal(JSON.parse(fifo.options.body).prompt, "queued input");
});

test("ambiguous steering retries keep exact UUID AND original run across activations, never implicitly Queue", async () => {
  const app = await boot();
  await enableSteering(app);
  app.steeringPosts.push(Promise.reject(new Error("connection lost after admission")));
  app.elements.get("prompt").value = "possibly accepted";
  await app.elements.get("steer").onclick();
  await enableSteering(app, 2, "run-new");
  await app.elements.get("composer").requestSubmit();
  assert(!app.requests.some((request) => request.path.endsWith("/runs")));
  await app.elements.get("steering-attempts").children[0].children[3].onclick();
  const posts = app.requests.filter((request) => request.path.endsWith("/steer") && request.options.method === "POST");
  assert.equal(posts.length, 2);
  assert.equal(posts[0].path, posts[1].path);
  assert.equal(posts[0].options.body, posts[1].options.body);
  assert.equal(app.elements.get("steering-status").textContent.includes("run-new"), true);
});

test("an ambiguous FIFO draft cannot silently become steering", async () => {
  const app = await boot();
  await enableSteering(app);
  app.submissions.push(response({ detail: "network failure" }, 503));
  app.elements.get("prompt").value = "bound FIFO";
  await app.elements.get("composer").requestSubmit();
  await app.elements.get("steer").onclick();
  assert(!app.requests.some((request) => request.path.endsWith("/steer") && request.options.method === "POST"));
  assert.equal(app.elements.get("prompt").value, "bound FIFO");
});

test("final-empty racing rejection preserves the draft, disables closure, and never falls back to FIFO", async () => {
  const app = await boot();
  await enableSteering(app);
  const state = app.steering.get("run-test");
  state.accepting = false;
  app.steeringPosts.push(response({ detail: "Run is closing; steering admission is sealed" }, 409));
  app.elements.get("prompt").value = "racing final empty";
  await app.elements.get("steer").onclick();
  assert.equal(app.elements.get("prompt").value, "racing final empty");
  assert.equal(app.elements.get("steer").disabled, true);
  assert(app.elements.get("steering-attempts").textContent.includes("Request rejected"));
  assert(!app.requests.some((request) => request.path.endsWith("/runs")));
});

test("durable capability and receipt events refresh exact snapshots without polling or child targeting", async () => {
  const app = await boot();
  app.steering.set("run-test", { run_id: "run-test", session_id: "ngn-test", run_status: "running", supported: false, accepting: false, receipts: [] });
  app.states.push(response({ runs: [{ id: "run-test", session_id: "ngn-test" }], approvals: [] }));
  app.emit(1, "run_started", { prompt: "active" });
  await flush();
  assert.equal(app.elements.get("steer").disabled, true);
  Object.assign(app.steering.get("run-test"), { supported: true, accepting: true });
  app.emit(2, "steering_updated", { input_id: "" });
  await flush();
  assert.equal(app.elements.get("steer").disabled, false);
  const task = { run_id: "child", session_id: "child-session", root_session_id: "ngn-test", title: "Child", depth: 1, status: "running" };
  app.tasks.push(task);
  app.states.push(response({ runs: [{ id: "run-test", session_id: "ngn-test" }], approvals: [] }));
  app.emit(3, "task_started", { task });
  await flush();
  app.elements.get("tasks").children[0].children[1].onclick();
  app.elements.get("prompt").value = "root only";
  await app.elements.get("steer").onclick();
  assert(app.requests.some((request) => request.path === "/api/runs/run-test/steer" && request.options.method === "POST"));
  assert(!app.requests.some((request) => request.path === "/api/runs/child/steer"));
});

test("stale same-run snapshots cannot undo terminal receipts and all statuses render as safe ordered text", async () => {
  const app = await boot();
  await enableSteering(app);
  const older = deferred(), newer = deferred();
  app.steerings.push(older.promise, newer.promise);
  app.emit(2, "steering_updated", { input_id: "receipt" });
  app.emit(3, "steering_updated", { input_id: "receipt" });
  const receipts = ["undelivered", "uncertain", "applied", "offered", "pending"].map((status, index) => ({ input_id: status, position: 5 - index, run_id: "run-test", session_id: "ngn-test", status, prompt: '<img src=x onerror="steal()">' }));
  const base = app.steering.get("run-test");
  newer.resolve(response({ steering: { ...base, receipts } }));
  await flush();
  older.resolve(response({ steering: { ...base, receipts: [] } }));
  await flush();
  const panel = app.elements.get("steering-receipts");
  assert.deepEqual(panel.children.map((row) => row.children[0].textContent.split(" · ")[1]), ["pending", "offered", "applied", "uncertain", "undelivered"]);
  assert(panel.textContent.includes("not proof of model consumption"));
  assert(panel.textContent.includes("may be persisted"));
  assert(panel.textContent.includes('<img src=x onerror="steal()">'));
  app.steering.set("run-test", { ...base, run_status: "cancelled", supported: false, accepting: false, receipts });
  app.emit(4, "run_finished", { status: "cancelled", error: "" });
  await flush();
  assert.equal(app.elements.get("steer").disabled, true);
  assert.equal(panel.children.length, 5);
  app.sockets[0].onopen();
  await flush();
  assert.equal(panel.children.length, 5);
  assert(!app.requests.some((request) => request.options.method === "POST"));
});

test("a delayed old-run steering response cannot clear a newer draft or replace the newer root", async () => {
  const app = await boot();
  await enableSteering(app);
  const accepted = deferred();
  app.steeringPosts.push(accepted.promise);
  app.elements.get("prompt").value = "first steering";
  const sending = app.elements.get("steer").onclick();
  const sent = app.requests.find((request) => request.path.endsWith("/steer") && request.options.method === "POST");
  app.elements.get("prompt").value = "newer draft";
  app.elements.get("prompt").oninput();
  await enableSteering(app, 2, "run-new");
  accepted.resolve(response({ receipt: { ...JSON.parse(sent.options.body), run_id: "run-test", session_id: "ngn-test", input_id: "saved", status: "applied" } }));
  await sending;
  assert.equal(app.elements.get("prompt").value, "newer draft");
  assert(app.elements.get("steering-status").textContent.includes("run-new"));
});

test("same-text newer edits and session switches are guarded against delayed accepted steering", async () => {
  const app = await boot();
  await enableSteering(app);
  const accepted = deferred();
  app.steeringPosts.push(accepted.promise);
  app.elements.get("prompt").value = "identical newer text";
  const sending = app.elements.get("steer").onclick();
  const sent = app.requests.find((request) => request.path.endsWith("/steer") && request.options.method === "POST");
  app.elements.get("prompt").oninput();
  await app.elements.get("mobile-new-session").onclick();
  accepted.resolve(response({ receipt: { ...JSON.parse(sent.options.body), run_id: "run-test", session_id: "ngn-test", input_id: "saved", status: "pending" } }));
  await sending;
  assert.equal(app.elements.get("prompt").value, "identical newer text");
  assert.equal(app.elements.get("steer").disabled, true);
  assert.equal(app.elements.get("steering-receipts").children.length, 0);
  assert.equal(app.elements.get("title").textContent, "New conversation");
});

test("foreign run/session receipt snapshots never enable steering or contaminate a newer session", async () => {
  const app = await boot();
  app.states.push(response({ runs: [{ id: "run-test", session_id: "ngn-test" }], approvals: [] }));
  app.emit(1, "run_started", { prompt: "unsupported" });
  await flush();
  app.steerings.push(response({ steering: { run_id: "child", session_id: "ngn-test", run_status: "running", supported: true, accepting: true, receipts: [] } }));
  app.emit(2, "steering_updated", { input_id: "" });
  await flush();
  assert.equal(app.elements.get("steer").disabled, true);
  const old = deferred();
  app.steerings.push(old.promise);
  app.emit(3, "steering_updated", { input_id: "old" });
  await app.elements.get("mobile-new-session").onclick();
  old.resolve(response({ steering: { run_id: "run-test", session_id: "ngn-test", run_status: "running", supported: true, accepting: true, receipts: [{ run_id: "run-test", session_id: "ngn-test", input_id: "old", position: 1, prompt: "old session", status: "pending" }] } }));
  await flush();
  assert.equal(app.elements.get("steer").disabled, true);
  assert.equal(app.elements.get("steering-receipts").children.length, 0);
  assert.equal(app.elements.get("mobile-sessions").value, app.catalog[0].id);
});

test("simultaneous steering gestures admit once, and accepted responses respect same-text newer revisions", async () => {
  const app = await boot();
  await enableSteering(app);
  const accepted = deferred();
  app.steeringPosts.push(accepted.promise);
  app.elements.get("prompt").value = "same-text edit";
  const sending = app.elements.get("steer").onclick();
  await app.elements.get("steer").onclick();
  app.elements.get("prompt").oninput();
  const posts = app.requests.filter((request) => request.path.endsWith("/steer") && request.options.method === "POST");
  assert.equal(posts.length, 1);
  accepted.resolve(response({ receipt: { ...JSON.parse(posts[0].options.body), run_id: "run-test", session_id: "ngn-test", input_id: "saved", status: "applied" } }));
  await sending;
  assert.equal(app.elements.get("prompt").value, "same-text edit");
  assert.equal(app.elements.get("steer").disabled, true);
  await app.elements.get("composer").requestSubmit();
  assert(!app.requests.some((request) => request.path.endsWith("/runs")));
  assert(app.elements.get("steering-attempts").textContent.includes("Clear bound draft"));
});

test("switching sessions never rebinds an ambiguous steering draft, including explicit retries from its receipt row", async () => {
  const app = await boot();
  await enableSteering(app);
  app.steeringPosts.push(response({ detail: "connection failed after possible admission" }, 503));
  app.elements.get("prompt").value = "ambiguous across sessions";
  await app.elements.get("steer").onclick();
  await app.elements.get("mobile-new-session").onclick();
  const other = app.catalog[0];
  app.steering.set("other-root", { run_id: "other-root", session_id: other.id, run_status: "running", supported: true, accepting: true, receipts: [] });
  app.states.push(response({ runs: [{ id: "other-root", session_id: other.id }], approvals: [] }));
  app.sockets.at(-1).onmessage({ data: JSON.stringify({ session_id: other.id, run_id: "other-root", sequence: 10, event: "run_started", prompt: "other active turn" }) });
  await flush();
  assert.equal(app.elements.get("steer").disabled, true);
  await app.elements.get("steer").onclick();
  await app.elements.get("composer").requestSubmit();
  assert(app.elements.get("steering-attempts").textContent.includes("Session ngn-test"));
  const row = app.elements.get("steering-attempts").children[0];
  await row.children[3].onclick();
  const posts = app.requests.filter((request) => request.path.endsWith("/steer") && request.options.method === "POST");
  assert.equal(posts.length, 2);
  assert.equal(posts[0].path, "/api/runs/run-test/steer");
  assert.equal(posts[0].path, posts[1].path);
  assert.equal(posts[0].options.body, posts[1].options.body);
  assert(!app.requests.some((request) => request.path.endsWith("/runs")));
  assert.equal(app.elements.get("prompt").value, "ambiguous across sessions");
  assert.equal(app.elements.get("steering-receipts").children.length, 0);
  assert(app.elements.get("steering-status").textContent.includes("other-root"));
});

test("releasing a definite host rejection is a separate local gesture, not an implicit FIFO conversion", async () => {
  const app = await boot();
  await enableSteering(app);
  app.steeringPosts.push(response({ detail: "Admission sealed" }, 409));
  app.elements.get("prompt").value = "host-rejected draft";
  await app.elements.get("steer").onclick();
  const count = app.requests.length;
  app.elements.get("steering-attempts").children[0].children[4].onclick();
  assert.equal(app.requests.length, count);
  assert.equal(app.elements.get("prompt").value, "host-rejected draft");
  assert.equal(app.elements.get("steering-attempts").children.length, 0);
});
