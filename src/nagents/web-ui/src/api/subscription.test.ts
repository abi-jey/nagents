import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { CREDENTIAL_TIMEOUT, HANDSHAKE_TIMEOUT, parseFrame, subscribeEvents, type EventFrame, type EventSocket, type Position, type SubscriptionOptions } from "./subscription.js";
import { readBootstrap } from "./bootstrap.js";
import { queueMessage } from "./messages.js";
import { deferred, dictationConfig } from "../features/dictation/testFixtures.js";
import type { Bootstrap } from "../types.js";

const snapshot = (cursor = 1, epoch = "epoch-a", session_id = "root") => ({
  type: "snapshot", session_id, cursor, epoch,
  snapshot: { session_id, history: [], sessions: [], retained_tasks: [], activity_cursor: 0, active_run: null },
});
function fixture(position?: Position, credentials: Pick<SubscriptionOptions, "refreshCredentials" | "credentials"> = {}) {
  const sockets: (EventSocket & { messages: string[]; closed: boolean; url: string; protocols: string[] })[] = [];
  const timers: { callback: () => void; delay: number; cancelled: boolean }[] = [];
  const frames: EventFrame[] = [];
  const states: string[] = [];
  const controller = new AbortController();
  const stop = subscribeEvents({
    ...credentials,
    token: "private-process-token", sessionId: "root", url: "https://localhost:8000/?ignored=yes", position,
    signal: controller.signal, receive: (frame) => frames.push(frame), status: (state) => states.push(state),
    socket: (url, protocols) => {
      assert.equal(sockets.filter((socket) => !socket.closed).length, 0, "No overlapping sockets");
      const socket = { url, protocols, messages: [] as string[], closed: false,
        send(data: string | ArrayBufferLike | Blob | ArrayBufferView) { this.messages.push(String(data)); },
        close() { this.closed = true; }, onopen: null, onclose: null, onerror: null, onmessage: null } as typeof sockets[number];
      sockets.push(socket); return socket;
    },
    schedule: (callback, delay) => {
      const timer = { callback, delay, cancelled: false }; timers.push(timer);
      return () => { timer.cancelled = true; };
    },
  });
  const emit = (data: unknown, socket = sockets.at(-1)!) => socket.onmessage?.call(socket as unknown as WebSocket, { data: JSON.stringify(data) } as MessageEvent);
  const opened = () => sockets.at(-1)!.onopen?.call(sockets.at(-1) as unknown as WebSocket, {} as Event);
  const disconnected = () => sockets.at(-1)!.onclose?.call(sockets.at(-1) as unknown as WebSocket, {} as CloseEvent);
  const tick = () => { const timer = timers.at(-1)!; if (!timer.cancelled) { timer.cancelled = true; timer.callback(); } };
  return { sockets, timers, frames, states, controller, stop, emit, opened, disconnected, tick };
}
test("WS authenticates with subprotocols, never a query, and subscribes with cursor plus epoch", () => {
  const f = fixture({ cursor: 41, epoch: "previous-instance" }); f.opened();
  assert.equal(f.sockets[0].url, "wss://localhost:8000/api/events");
  assert.deepEqual(f.sockets[0].protocols, ["ngn.events.v1", "ngn.token.private-process-token"]);
  assert.deepEqual(JSON.parse(f.sockets[0].messages[0]), { type: "subscribe", session_id: "root", after: 41, epoch: "previous-instance" });
  f.stop();
});
test("subscription drops duplicate and wrong-root records; reconnect resumes the last received position", () => {
  const f = fixture(); f.opened(); f.emit(snapshot());
  const event = { type: "event", session_id: "root", cursor: 4, epoch: "epoch-a", record: { event: "text_chunk", chunk: "once", run_id: "run", task_id: "child", activation: 3 } };
  f.emit(event); f.emit(event); f.emit({ ...event, cursor: 8, session_id: "other" });
  assert.equal(f.frames.length, 2);
  f.disconnected(); assert.equal(f.sockets[0].closed, true); f.tick(); f.opened();
  assert.deepEqual(JSON.parse(f.sockets[1].messages[0]), { type: "subscribe", session_id: "root", after: 4, epoch: "epoch-a" });
  assert.deepEqual(f.frames[1], event, "Task/activation/run scope is not rewritten"); f.stop();
});
test("instance changes and malformed scope force a snapshot subscription instead of ingesting uncertain chunks", () => {
  const f = fixture(); f.emit(snapshot());
  f.emit({ type: "event", session_id: "root", cursor: 3, epoch: "restarted", record: { event: "text_chunk", chunk: "unknown" } });
  assert.equal(f.frames.length, 1); f.tick(); f.opened();
  assert.deepEqual(JSON.parse(f.sockets[1].messages[0]), { type: "subscribe", session_id: "root", after: 0 });
  f.emit(snapshot(0, "restarted"));
  f.emit({ type: "event", session_id: "root", cursor: 4, epoch: "restarted", record: { event: "approval", activation: "3" } });
  assert.equal(f.frames.length, 2); assert.equal(f.sockets[1].closed, true); f.stop();
});
test("bounded reconnect backoff is cancellable and detached callbacks cannot revive a selected-away socket", () => {
  const f = fixture();
  for (let i = 0; i < 10; i++) { f.disconnected(); f.tick(); }
  const retryDelays = f.timers.filter((timer) => timer.delay !== HANDSHAKE_TIMEOUT).map((timer) => timer.delay);
  assert.equal(retryDelays[0], 500); assert.equal(retryDelays.at(-1), 15000);
  f.disconnected(); f.controller.abort(); const count = f.sockets.length; f.tick();
  assert.equal(f.sockets.length, count); assert.equal(f.timers.at(-1)?.cancelled, true);
  assert.ok(f.sockets.every((socket) => socket.closed && socket.onmessage === null));
});
test("snapshot validation checks histories, scoped replay and pending child approval records", () => {
  const frame = snapshot();
  assert.equal(parseFrame(JSON.stringify(frame)).type, "snapshot");
  for (const broken of [null, { ...frame, cursor: -1 }, { ...frame, snapshot: { ...frame.snapshot, history: [{}] } },
    { ...frame, snapshot: { ...frame.snapshot, active_run: { id: "run", status: "running", pending_approvals: [{ event: "approval", task_id: 7 }] } } }])
    assert.throws(() => parseFrame(JSON.stringify(broken)));
});
test("backend status and normalized active records accept empty approvals without discarding scoped history", () => {
  assert.equal(parseFrame(JSON.stringify({ type: "status", epoch: "one", cursor: 3, active_run_id: "run", active_session_id: "other" })).type, "status");
  const frame = snapshot();
  assert.equal(parseFrame(JSON.stringify({ ...frame, snapshot: { ...frame.snapshot, active_run: {
    id: "run", status: "running", approval: {}, records: [{ event: "text_chunk", chunk: "partial", run_id: "run", task_id: "child", activation: 4 }],
  } } })).type, "snapshot");
  assert.throws(() => parseFrame(JSON.stringify({ type: "event", cursor: 3, epoch: "one", session_id: "root", record: { event: "approval", session_id: "other" } })));
});
test("empty reconnect replay requests one WS snapshot without starting an HTTP poll", () => {
  const f = fixture({ cursor: 9, epoch: "one" }); f.opened();
  f.emit({ type: "sessions", cursor: 10, epoch: "one", sessions: [] });
  f.tick();
  assert.deepEqual(JSON.parse(f.sockets[0].messages[1]), { type: "subscribe", session_id: "root", after: 0 });
  f.emit(snapshot(10, "one"));
  assert.equal(f.frames.at(-1)?.type, "snapshot", "An equal-cursor requested snapshot still reconciles pending approvals");
  assert.equal(f.sockets.length, 1); f.stop();
});

function bootstrap(token = "private-process-token"): Bootstrap {
  return { token, workspace: "/mock/workspace", provider: "mock", agent: "main", model: "mock-model", demo: true,
    active_run_id: "", active_session_id: "different-server-selection", dictation: { ...dictationConfig } };
}

test("failed old-token handshake refreshes bootstrap before the next socket and subsequent HTTP admission", async (context) => {
  let current = bootstrap();
  const calls: string[] = [];
  const errors = context.mock.method(console, "error", () => undefined);
  const f = fixture({ cursor: 42, epoch: "old-server" }, {
    refreshCredentials: readBootstrap, credentials: (next) => { current = next; },
  });
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    calls.push(url);
    if (url === "/api/bootstrap") {
      assert.ok(f.sockets.every((socket) => socket.closed), "Credential refresh must not overlap a socket");
      assert.equal(options.method, "GET"); assert.deepEqual(options.headers, {});
      assert.equal(options.cache, "no-store"); assert.equal(options.credentials, "same-origin");
      assert.ok(options.signal); assert.equal(options.signal.aborted, false);
      return Response.json(bootstrap("rotated-process-token"));
    }
    assert.equal(url, "/api/messages");
    assert.equal((options.headers as Record<string, string>)["X-Ngn-Token"], "rotated-process-token");
    const message = JSON.parse(options.body as string) as { session_id: string; message_id: string; prompt: string };
    assert.equal(message.session_id, "root"); assert.equal(message.prompt, "User's next explicit message");
    return Response.json({ session_id: message.session_id, message_id: message.message_id, status: "queued" });
  });
  const old = f.sockets[0];
  const lateClose = old.onclose;
  old.onerror?.call(old as unknown as WebSocket, {} as Event);
  lateClose?.call(old as unknown as WebSocket, {} as CloseEvent);
  assert.equal(f.timers.filter((timer) => !timer.cancelled).length, 1);
  assert.equal(f.timers.at(-1)?.delay, 500);
  f.tick(); await setImmediate();
  assert.equal(f.sockets.length, 2); assert.equal(current.token, "rotated-process-token");
  assert.deepEqual(f.sockets[1].protocols, ["ngn.events.v1", "ngn.token.rotated-process-token"]);
  assert.equal(f.sockets[1].url, "wss://localhost:8000/api/events");
  f.opened();
  assert.deepEqual(JSON.parse(f.sockets[1].messages[0]), { type: "subscribe", session_id: "root", after: 0 });
  f.emit(snapshot(1, "new-server"));
  assert.deepEqual(calls, ["/api/bootstrap"], "Recovery cannot retry a model request");
  await queueMessage(current.token, { session_id: "root", message_id: "explicit-uuid", prompt: "User's next explicit message" });
  assert.deepEqual(calls, ["/api/bootstrap", "/api/messages"]);
  assert.equal(errors.mock.callCount(), 0);
  assert.doesNotMatch(JSON.stringify(f.states), /private-process-token|rotated-process-token/);
  f.stop();
});

test("aborting during credential refresh ignores late credentials, socket callbacks and timers", async (context) => {
  const response = deferred<Response>();
  let signal: AbortSignal | undefined;
  const accepted: Bootstrap[] = [];
  context.mock.method(globalThis, "fetch", async (_url: string, options: RequestInit) => {
    signal = options.signal as AbortSignal; return response.promise;
  });
  const f = fixture(undefined, { refreshCredentials: readBootstrap, credentials: (next) => { accepted.push(next); } });
  const oldOpen = f.sockets[0].onopen;
  f.disconnected();
  const oldRetry = f.timers.at(-1)!.callback;
  f.tick(); await setImmediate();
  assert.ok(signal); assert.equal(signal.aborted, false);
  f.controller.abort();
  assert.equal(signal.aborted, true);
  response.resolve(Response.json(bootstrap("late-token")));
  oldOpen?.call(f.sockets[0] as unknown as WebSocket, {} as Event);
  oldRetry(); await setImmediate();
  assert.equal(accepted.length, 0); assert.equal(f.sockets.length, 1);
  assert.ok(f.sockets.every((socket) => socket.closed));
  assert.equal(f.timers.filter((timer) => !timer.cancelled).length, 0);
});

test("unchanged bootstrap tokens retain cursor and capped backoff rather than restarting a tight handshake loop", async (context) => {
  let reads = 0;
  let notifications = 0;
  const f = fixture({ cursor: 12, epoch: "existing-epoch" }, {
    refreshCredentials: readBootstrap, credentials: () => { notifications++; },
  });
  context.mock.method(globalThis, "fetch", async (url: string) => {
    assert.equal(url, "/api/bootstrap"); assert.ok(f.sockets.every((socket) => socket.closed));
    reads++; return Response.json(bootstrap());
  });
  const delays: number[] = [];
  for (let index = 0; index < 8; index++) {
    f.disconnected(); delays.push(f.timers.at(-1)!.delay);
    f.tick(); await setImmediate();
    f.opened();
    assert.deepEqual(JSON.parse(f.sockets.at(-1)!.messages[0]), { type: "subscribe", session_id: "root", after: 12, epoch: "existing-epoch" });
  }
  assert.deepEqual(delays, [500, 1000, 2000, 4000, 8000, 15000, 15000, 15000]);
  assert.equal(reads, 8); assert.equal(notifications, 8);
  f.stop(); assert.equal(f.timers.filter((timer) => !timer.cancelled).length, 0);
});

test("a bounded bootstrap timeout aborts its request and a later response cannot overwrite a recovered token", async (context) => {
  const held = deferred<Response>();
  const signals: AbortSignal[] = [];
  const accepted: string[] = [];
  const f = fixture(undefined, { refreshCredentials: readBootstrap, credentials: (next) => { accepted.push(next.token); } });
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    assert.equal(url, "/api/bootstrap"); assert.ok(f.sockets.every((socket) => socket.closed));
    assert.ok(signals.every((signal) => signal.aborted), "Only one credential read may be active");
    signals.push(options.signal as AbortSignal);
    return signals.length === 1 ? held.promise : Response.json(bootstrap("fresh-token"));
  });
  f.disconnected(); f.tick(); await setImmediate();
  assert.equal(f.timers.at(-1)?.delay, CREDENTIAL_TIMEOUT);
  f.tick(); await setImmediate();
  assert.equal(signals[0].aborted, true); assert.equal(f.sockets.length, 1);
  assert.equal(f.timers.at(-1)?.delay, 1000);
  f.tick(); await setImmediate();
  assert.equal(f.sockets.length, 2); assert.deepEqual(accepted, ["fresh-token"]);
  held.resolve(Response.json(bootstrap("obsolete-late-token"))); await setImmediate();
  assert.deepEqual(accepted, ["fresh-token"]);
  assert.deepEqual(f.sockets[1].protocols, ["ngn.events.v1", "ngn.token.fresh-token"]);
  f.stop(); assert.equal(f.timers.filter((timer) => !timer.cancelled).length, 0);
});

test("a stalled WebSocket handshake times out into credential recovery without waiting for an epoch frame", async () => {
  let reads = 0;
  const f = fixture(undefined, { refreshCredentials: async () => { reads++; return bootstrap("recovered-token"); } });
  assert.equal(f.timers.at(-1)?.delay, HANDSHAKE_TIMEOUT);
  f.tick(); assert.equal(f.sockets[0].closed, true);
  assert.equal(f.timers.at(-1)?.delay, 500);
  f.tick(); await setImmediate();
  assert.equal(reads, 1); assert.equal(f.sockets.length, 2);
  assert.deepEqual(f.sockets[1].protocols, ["ngn.events.v1", "ngn.token.recovered-token"]);
  f.stop();
});

test("failed or malformed bootstrap responses expose no token, body or raw exception", async (context) => {
  let response = Response.json({ detail: "credential=do-not-expose" }, { status: 503 });
  context.mock.method(globalThis, "fetch", async () => response);
  for (const next of [response, Response.json({ ...bootstrap(), token: "do-not-expose\ninvalid" }), Response.json({ token: "do-not-expose" })]) {
    response = next;
    await assert.rejects(readBootstrap(new AbortController().signal), (cause: Error) => {
      assert.match(cause.message, /credentials could not be refreshed/);
      assert.doesNotMatch(cause.message, /do-not-expose|503|invalid/); return true;
    });
  }
});
