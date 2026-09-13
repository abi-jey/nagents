import type { ActiveRun, Bootstrap, Session, Snapshot, WireEvent } from "../types.js";

export type Position = { cursor: number; epoch: string };
export type EventFrame = Position & (
  | { type: "snapshot"; session_id: string; snapshot: Snapshot }
  | { type: "event"; session_id: string; record: WireEvent }
  | { type: "sessions"; sessions: Session[]; active_run?: ActiveRun | null }
  | { type: "status"; active_session_id: string; active_run_id: string }
);
export type SessionFrame = Extract<EventFrame, { type: "event" | "snapshot" }>;

export function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}
function integer(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}
export function validRecord(value: unknown): value is WireEvent {
  return object(value) && typeof value.event === "string" &&
    ["session_id", "run_id", "task_id", "call_id", "id", "approval_id", "parent_task_id", "message_id"].every(
      (key) => value[key] === undefined || typeof value[key] === "string",
    ) && ["activation", "followup", "depth"].every((key) => value[key] === undefined || integer(value[key]));
}
function sessions(value: unknown): value is Session[] {
  return Array.isArray(value) && value.every((item: unknown) => object(item) &&
    ["id", "title", "updated_at"].every((key) => typeof item[key] === "string") &&
    ["active_run_id", "parent_session_id", "status"].every((key) => item[key] === undefined || typeof item[key] === "string"));
}
function activeRun(value: unknown): boolean {
  return value === undefined || value === null || (object(value) &&
    typeof value.id === "string" && typeof value.status === "string" &&
    (value.session_id === undefined || typeof value.session_id === "string") &&
    ["events", "records", "pending_approvals"].every((key) => value[key] === undefined ||
      (Array.isArray(value[key]) && value[key].every(validRecord))) &&
    (value.approval === undefined || (object(value.approval) && !Object.keys(value.approval).length) || validRecord(value.approval)));
}
export function validSnapshot(value: unknown): value is Snapshot {
  return object(value) && typeof value.session_id === "string" && sessions(value.sessions) &&
    (value.activity_cursor === undefined || integer(value.activity_cursor)) && activeRun(value.active_run) &&
    Array.isArray(value.history) && value.history.every((item: unknown) => object(item) &&
      ["role", "content", "name", "tool_call_id"].every((key) => typeof item[key] === "string") &&
      (item.message_id === undefined || typeof item.message_id === "string") &&
      Array.isArray(item.tool_calls) && item.tool_calls.every((call: unknown) => object(call) &&
        typeof call.id === "string" && typeof call.name === "string")) &&
    Array.isArray(value.retained_tasks) && value.retained_tasks.every((item: unknown) => object(item) &&
      ["id", "name", "status", "session_id", "parent_task_id", "prompt", "result", "error"].every(
        (key) => typeof item[key] === "string") && integer(item.activation) && integer(item.followups));
}
export function parseFrame(data: unknown): EventFrame {
  if (typeof data !== "string" || data.length > 8 * 1024 * 1024) throw new Error("Invalid event frame.");
  const value: unknown = JSON.parse(data);
  if (!object(value) || !integer(value.cursor) || typeof value.epoch !== "string" || !value.epoch)
    throw new Error("Invalid event position.");
  if (value.type === "sessions" && sessions(value.sessions) && activeRun(value.active_run)) return value as EventFrame;
  if (value.type === "status" && typeof value.active_session_id === "string" && typeof value.active_run_id === "string") return value as EventFrame;
  if (typeof value.session_id !== "string") throw new Error("Invalid event session.");
  if (value.type === "event" && validRecord(value.record) &&
      (value.record.session_id === undefined || value.record.session_id === value.session_id)) return value as EventFrame;
  if (value.type === "snapshot" && validSnapshot(value.snapshot) && value.snapshot.session_id === value.session_id)
    return value as EventFrame;
  throw new Error("Invalid event payload.");
}

export type EventSocket = Pick<WebSocket, "send" | "close" | "onopen" | "onmessage" | "onerror" | "onclose">;
export type SubscriptionOptions = {
  token: string;
  sessionId: string;
  position?: Position;
  url: string;
  signal: AbortSignal;
  receive: (frame: EventFrame) => void;
  status: (status: "connecting" | "connected" | "reconnecting") => void;
  refreshCredentials?: (signal: AbortSignal) => Promise<Bootstrap>;
  credentials?: (bootstrap: Bootstrap) => void;
  socket?: (url: string, protocols: string[]) => EventSocket;
  schedule?: (callback: () => void, delay: number) => () => void;
};

export const HANDSHAKE_TIMEOUT = 10000;
export const CREDENTIAL_TIMEOUT = 5000;

// A late or noncooperative refresh must not hold the reconnect loop forever or
// deliver credentials after abort. The underlying HTTP request also gets signal.
function refreshWithAbort(read: (signal: AbortSignal) => Promise<Bootstrap>, signal: AbortSignal): Promise<Bootstrap> {
  return new Promise((resolve, reject) => {
    const abort = () => reject(new DOMException("Credential refresh cancelled.", "AbortError"));
    if (signal.aborted) { abort(); return; }
    signal.addEventListener("abort", abort, { once: true });
    void Promise.resolve().then(() => { signal.throwIfAborted(); return read(signal); }).then(
      (value) => { signal.removeEventListener("abort", abort); if (!signal.aborted) resolve(value); },
      () => { signal.removeEventListener("abort", abort); reject(new Error("Credential refresh unavailable.")); },
    );
  });
}

// Socket, credential read, and retry wait are mutually exclusive. Recovery never
// starts an HTTP history poll, retries a model run, or sends a cancellation.
export function subscribeEvents(options: SubscriptionOptions): () => void {
  let token = options.token;
  let position = options.position;
  let socket: EventSocket | undefined;
  let cancelTimer: (() => void) | undefined;
  let cancelHydration: (() => void) | undefined;
  let cancelHandshake: (() => void) | undefined;
  let cancelCredentialTimeout: (() => void) | undefined;
  let refreshing: AbortController | undefined;
  let awaitingSnapshot = false;
  let stopped = false;
  let failures = 0;
  const schedule = options.schedule || ((callback, delay) => {
    const timer = setTimeout(callback, delay);
    return () => clearTimeout(timer);
  });
  function detach() {
    cancelHydration?.(); cancelHydration = undefined;
    cancelHandshake?.(); cancelHandshake = undefined;
    if (!socket) return;
    socket.onopen = socket.onmessage = socket.onclose = socket.onerror = null;
    socket.close();
    socket = undefined;
  }
  function retry() {
    if (stopped || cancelTimer || refreshing) return;
    detach();
    options.status("reconnecting");
    cancelTimer = schedule(() => { cancelTimer = undefined; reconnect(); }, Math.min(500 * 2 ** Math.min(failures++, 6), 15000));
  }
  function finishRefresh() {
    cancelCredentialTimeout?.(); cancelCredentialTimeout = undefined;
    refreshing = undefined;
  }
  function reconnect() {
    if (stopped || refreshing) return;
    if (!options.refreshCredentials) { connect(); return; }
    const controller = new AbortController();
    refreshing = controller;
    cancelCredentialTimeout = schedule(() => controller.abort(), CREDENTIAL_TIMEOUT);
    void refreshWithAbort(options.refreshCredentials, controller.signal).then((bootstrap) => {
      if (stopped || refreshing !== controller || controller.signal.aborted) return;
      if (bootstrap.token !== token) position = undefined;
      token = bootstrap.token;
      options.credentials?.(bootstrap);
      // The owner may stop or replace this subscription while accepting metadata.
      if (stopped || refreshing !== controller || controller.signal.aborted) return;
      finishRefresh();
      connect();
    }).catch(() => {
      if (stopped || refreshing !== controller) return;
      finishRefresh();
      retry();
    });
  }
  function connect() {
    if (stopped || refreshing || socket) return;
    options.status(failures ? "reconnecting" : "connecting");
    if (stopped) return;
    try {
      const url = new URL("/api/events", options.url);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      socket = (options.socket || ((url, protocols) => new WebSocket(url, protocols)))(
        url.href, ["ngn.events.v1", `ngn.token.${token}`],
      );
      const connection = socket;
      cancelHandshake = schedule(() => { if (socket === connection) retry(); }, HANDSHAKE_TIMEOUT);
      socket.onopen = () => {
        if (stopped || socket !== connection) return;
        cancelHandshake?.(); cancelHandshake = undefined;
        socket?.send(JSON.stringify({ type: "subscribe", session_id: options.sessionId,
          after: position?.cursor || 0, ...(position?.epoch ? { epoch: position.epoch } : {}) }));
        options.status("connected");
        if (stopped || socket !== connection) return;
        // A valid replay cursor can yield no frames at all. Reconcile idle history
        // and pending approvals once if the server has no selected-root reply.
        cancelHydration = schedule(() => {
          if (stopped || socket !== connection) return;
          cancelHydration = undefined; awaitingSnapshot = true;
          socket?.send(JSON.stringify({ type: "subscribe", session_id: options.sessionId, after: 0 }));
        }, 1500);
      };
      socket.onmessage = (event) => {
        if (stopped || socket !== connection) return;
        try {
          const frame = parseFrame(event.data);
          cancelHandshake?.(); cancelHandshake = undefined;
          const scoped = frame.type === "event" || frame.type === "snapshot";
          if (scoped && frame.session_id !== options.sessionId) return;
          if (!scoped && !position) {
            options.receive(frame);
            return; // A catalog frame cannot stand in for the selected history snapshot.
          }
          if (frame.type !== "snapshot" && (!position || position.epoch !== frame.epoch)) {
            position = undefined;
            retry();
            return;
          }
          if (position?.epoch === frame.epoch && (frame.cursor < position.cursor ||
              (frame.cursor === position.cursor && !(frame.type === "snapshot" && awaitingSnapshot)))) return;
          options.receive(frame);
          position = { cursor: frame.cursor, epoch: frame.epoch };
          if (scoped) { cancelHydration?.(); cancelHydration = undefined; awaitingSnapshot = false; }
          failures = 0;
          options.status("connected");
        } catch {
          // Do not echo malformed payloads, which can include private content.
          position = undefined;
          retry();
        }
      };
      socket.onclose = () => { if (socket === connection) retry(); };
      socket.onerror = () => { if (socket === connection) retry(); };
    } catch { retry(); }
  }
  function stop() {
    stopped = true;
    cancelTimer?.();
    cancelTimer = undefined;
    refreshing?.abort();
    finishRefresh();
    detach();
    options.signal.removeEventListener("abort", stop);
  }
  options.signal.addEventListener("abort", stop, { once: true });
  if (options.signal.aborted) stop();
  else connect();
  return stop;
}
