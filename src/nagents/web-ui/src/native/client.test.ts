import assert from "node:assert/strict";
import { test, type TestContext } from "node:test";
import { setTimeout as delay } from "node:timers/promises";
import { activeRun, NativeClient, pendingApproval, steeringTarget } from "./client.js";
import type { Approval, Event, Queue, Run, Snapshot, SteeringReceipt, SteeringState, Task, Transport } from "./transport.js";

type Request = { path: string; body?: object; token?: string };
type Fields<T> = T extends Event ? Omit<T, "schema_version" | "sequence" | "run_id" | "session_id" | "timestamp_ms"> : never;
const task: Task = {
  run_id: "child-run", session_id: "child", parent_run_id: "root", parent_session_id: "chat", root_run_id: "root",
  root_session_id: "chat", call_id: "delegate", title: "Reviewer", depth: 1, read_only: true, status: "running",
};
const childApproval: Approval = {
  id: "approval", run_id: task.run_id, session_id: task.session_id, root_session_id: task.root_session_id,
  agent_title: task.title, depth: task.depth, call: { call_id: "call", name: "write", arguments: { path: "file" } }, preview: "",
};
const run = (id: string, session_id = "chat"): Run => ({ id, session_id, request_id: "request", status: "running" });
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(yes => { resolve = yes; });
  return { promise, resolve };
}

async function until(condition: () => boolean) {
  for (let index = 0; index < 100; index++) { if (condition()) return; await delay(10); }
  assert(condition(), "timed out waiting for the client snapshot");
}

async function setup(t: TestContext) {
  const requests: Request[] = [];
  const streams: { id: string; receive: (event: Event) => void; connected: (open: boolean) => void; closed: boolean }[] = [];
  const state = { runs: [] as Run[], approvals: [] as Approval[], tasks: [] as Task[],
    queue: { session_id: "chat", paused: false, entries: [] } as Queue,
    steering: new Map<string, SteeringState>(), handler: undefined as ((request: Request) => unknown) | undefined };
  const transport: Transport = {
    async request<T>(path: string, token?: string, body?: object) {
      const request = { path, token, body };
      requests.push(request);
      assert.equal(token, path === "/api/bootstrap" ? undefined : "host-token");
      const custom = state.handler?.(request);
      if (custom !== undefined) return await custom as T;
      const parts = path.split("/");
      if (path === "/api/bootstrap") return { token: "host-token", protocol_version: 1 } as T;
      if (path === "/api/sessions") return (body ? { session: { id: "new", title: "New session" } } :
        { sessions: [{ id: "chat", title: "Test" }, { id: "other", title: "Other" }] }) as T;
      if (path === "/api/state") return { runs: state.runs, approvals: state.approvals } as T;
      if (parts[4] === "tasks") return { tasks: state.tasks } as T;
      if (parts[4] === "queue") return { queue: { ...state.queue, session_id: parts[3] }, limit: 32 } as T;
      if (parts[4] === "steer") return { steering: state.steering.get(parts[3]) } as T;
      if (parts[4] === "runs") return { run: { ...run("receipt", parts[3]), ...body } } as T;
      if (path === "/api/approvals" || parts[4] === "cancel") return {} as T;
      throw new Error(`Unexpected request: ${path}`);
    },
    watch(id, token, receive, connected) {
      assert.equal(token, "host-token");
      const stream = { id, receive, connected, closed: false };
      streams.push(stream);
      connected(true);
      return () => { stream.closed = true; };
    },
  };
  let next = 0, sequence = 0;
  const client = new NativeClient(transport, () => `request-${++next}`);
  t.after(() => client.dispose());
  await client.connect();
  await until(() => !!client.snapshot().queue);
  function emit(event: Fields<Event>, run_id = "root", session_id = "chat") {
    streams.findLast(stream => stream.id === session_id && !stream.closed)?.receive({
      ...event, run_id, session_id, schema_version: 1, sequence: ++sequence, timestamp_ms: 0,
    });
  }
  return { client, state, requests, streams, emit };
}

function capable(id: string, session = "chat"): SteeringState {
  return { run_id: id, session_id: session, run_status: "running", accepting: true, supported: true, receipts: [] };
}

test("replayed approvals do not open a decision; live child approval and inspection use exact identities", async t => {
  const { client, state, requests, emit, streams } = await setup(t);
  emit({ event: "approval_requested", approval_id: "expired", call: childApproval.call, preview: "" });
  await until(() => requests.filter(request => request.path === "/api/state").length >= 2);
  assert.equal(pendingApproval(client.snapshot()), undefined);

  state.tasks = [task, { ...task, run_id: "foreign", session_id: "foreign", root_session_id: "other" }];
  state.approvals = [childApproval];
  emit({ event: "task_started", task });
  await until(() => pendingApproval(client.snapshot())?.id === childApproval.id && client.snapshot().tasks.length === 1);
  client.inspect("child");
  emit({ event: "text_completed", text: "Child only" }, task.run_id, task.session_id);
  await until(() => client.snapshot().child?.entries[0]?.text === "Child only");
  assert(client.snapshot().entries.every(entry => entry.text !== "Child only"));
  await client.cancel(task.run_id);
  assert(requests.some(({ path }) => path === "/api/runs/child-run/cancel"));
  await client.decide({ ...childApproval, run_id: "root" }, true);
  assert(!requests.some(({ path }) => path === "/api/approvals"));
  await client.decide(childApproval, false);
  assert.deepEqual(requests.find(({ path }) => path === "/api/approvals")?.body,
    { run_id: task.run_id, approval_id: childApproval.id, allow: false });
  client.select("other");
  assert(streams.filter(stream => stream.id !== "other").every(stream => stream.closed));
  assert(!client.snapshot().child);
});

test("older state and queue replies do not resurrect an approval or leak queue data into another session", async t => {
  const { client, state, requests, emit } = await setup(t);
  const oldState = deferred<Snapshot>(), oldQueue = deferred<{ queue: Queue; limit: number }>();
  state.handler = ({ path }) => path === "/api/state" ? oldState.promise : path === "/api/sessions/chat/queue" ? oldQueue.promise : undefined;
  const before = requests.length;
  emit({ event: "run_started", prompt: "Running" });
  await until(() => requests.length >= before + 3);
  state.handler = undefined;
  emit({ event: "run_finished", status: "completed", error: "" });
  await until(() => requests.slice(before).filter(({ path }) => path === "/api/state").length === 2);
  oldState.resolve({ runs: [run("expired")], approvals: [childApproval] });
  client.select("other");
  oldQueue.resolve({ queue: { ...state.queue, paused: true }, limit: 32 });
  await until(() => client.snapshot().queue?.session_id === "other");
  assert(!client.snapshot().queue?.paused);
  assert(!client.snapshot().approvals.length);
  assert(!activeRun(client.snapshot()));
});

test("reconnecting never shows an approval from the prior connection", async t => {
  const { client, state, emit, streams, requests } = await setup(t);
  state.approvals = [childApproval];
  emit({ event: "approval_requested", approval_id: childApproval.id, call: childApproval.call, preview: "" });
  await until(() => pendingApproval(client.snapshot())?.id === childApproval.id);
  const oldState = deferred<Snapshot>(), newState = deferred<Snapshot>();
  const before = requests.length;
  state.handler = ({ path }) => path === "/api/state" ? oldState.promise : undefined;
  emit({ event: "task_updated", task });
  await until(() => requests.slice(before).some(({ path }) => path === "/api/state"));

  const stream = streams[0];
  stream.connected(false);
  assert.equal(pendingApproval(client.snapshot()), undefined);
  oldState.resolve({ runs: [], approvals: [childApproval] });
  await delay(0);
  assert.equal(pendingApproval(client.snapshot()), undefined);

  state.handler = ({ path }) => path === "/api/state" ? newState.promise : undefined;
  stream.connected(true);
  assert.equal(pendingApproval(client.snapshot()), undefined);
  const nextApproval = { ...childApproval, id: "new-approval" };
  newState.resolve({ runs: [], approvals: [nextApproval] });
  await until(() => pendingApproval(client.snapshot())?.id === nextApproval.id);
});

test("retry sends the same FIFO UUID; an accepted response cannot clear a newer edit", async t => {
  const { client, state, requests } = await setup(t);
  const path = "/api/sessions/chat/runs";
  client.setDraft("my command");
  state.handler = request => { if (request.path === path) throw new Error("Lost response"); };
  await client.send();
  assert.equal(client.snapshot().draft, "my command");
  const original = requests.find(request => request.path === path)?.body;
  const received = deferred<{ run: Run; warning: string }>();
  state.handler = request => request.path === path ? received.promise : undefined;
  const first = client.send();
  await client.send();
  client.setDraft("new draft");
  client.setDraft("my command");
  received.resolve({ run: { ...run("accepted"), request_id: "request-1" }, warning: "dispatch failed" });
  await first;
  assert.deepEqual(requests.filter(request => request.path === path).map(request => request.body), [original, original]);
  assert.equal(client.snapshot().draft, "my command");
  assert.match(client.snapshot().warning, /Input accepted/);
});

test("queue controls and removal preserve the exact conversation and pending run", async t => {
  const { client, state, emit, requests } = await setup(t);
  state.queue = { session_id: "chat", paused: true, entries: [{ run: { ...run("pending"), status: "queued" }, prompt: "Later" }] };
  emit({ event: "queue_updated", paused: true }, "");
  await until(() => client.snapshot().queue?.entries.length === 1);
  await client.cancel("unrelated");
  await client.cancel("pending");
  await client.controlQueue("resume");
  assert.deepEqual(requests.filter(request => request.body).map(({ path, body }) => [path, body]), [
    ["/api/runs/pending/cancel", {}], ["/api/sessions/chat/queue", { action: "resume" }],
  ]);
  assert(client.snapshot().entries.every(entry => entry.kind !== "user"));
});

test("steering requires a matching active-root snapshot and retries an ambiguous input on its original run only", async t => {
  const { client, state, emit, requests } = await setup(t);
  state.runs = [run("first")];
  state.steering.set("first", capable("first", "other"));
  emit({ event: "run_started", prompt: "Start" }, "first");
  await until(() => requests.some(request => request.path === "/api/runs/first/steer"));
  assert.equal(steeringTarget(client.snapshot()), undefined);
  state.steering.set("first", capable("first"));
  emit({ event: "steering_updated", input_id: "" }, "first");
  await until(() => steeringTarget(client.snapshot()) === "first");
  client.setDraft("change direction");
  state.handler = request => { if (request.path === "/api/runs/first/steer" && request.body) throw new Error("Lost response"); };
  await client.send(undefined, true);
  const binding = client.snapshot().bindings[0];
  assert.equal(binding.run, "first");
  assert.equal(binding.state, "unconfirmed");

  state.runs = [run("second")];
  state.steering.set("second", capable("second"));
  emit({ event: "run_started", prompt: "Next" }, "second");
  await until(() => steeringTarget(client.snapshot()) === "second");
  await client.send(); // Cannot implicitly turn this draft into FIFO.
  await client.send(undefined, true); // Cannot retarget this draft to another activation.
  const receipt: SteeringReceipt = { input_id: "applied", run_id: "first", session_id: "chat",
    request_id: binding.id, position: 1, prompt: binding.prompt, status: "applied" };
  state.handler = request => request.path === "/api/runs/first/steer" && request.body ? { receipt } : undefined;
  await client.retry(binding.id);
  assert.deepEqual(requests.filter(request => request.body).map(({ path, body }) => [path, body]), [
    ["/api/runs/first/steer", { prompt: binding.prompt, request_id: binding.id }],
    ["/api/runs/first/steer", { prompt: binding.prompt, request_id: binding.id }],
  ]);
  assert.equal(client.snapshot().draft, "");
});

test("only an exact terminal snapshot can release a rejected steering draft; nothing is sent on release", async t => {
  const { client, state, emit, requests } = await setup(t);
  state.runs = [run("active")];
  state.steering.set("active", capable("active"));
  emit({ event: "run_started", prompt: "Start" }, "active");
  await until(() => steeringTarget(client.snapshot()) === "active");
  state.handler = request => { if (request.path === "/api/runs/active/steer" && request.body) throw new Error("Unconfirmed"); };
  client.setDraft("Keep this");
  await client.send(undefined, true);
  const { id } = client.snapshot().bindings[0];
  client.release(id);
  assert.equal(client.snapshot().bindings[0]?.state, "unconfirmed");
  state.runs = [];
  state.steering.set("active", { ...capable("active"), run_status: "completed", accepting: false });
  emit({ event: "run_finished", status: "completed", error: "" }, "active");
  await until(() => client.snapshot().bindings[0]?.state === "rejected");
  client.release(id);
  assert.equal(client.snapshot().draft, "Keep this");
  assert(!client.snapshot().bindings.length);
  assert.deepEqual(requests.filter(request => request.body).map(request => request.path), ["/api/runs/active/steer"]);
});
