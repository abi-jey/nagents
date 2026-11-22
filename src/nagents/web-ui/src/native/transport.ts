// The Rust host owns these identities. This is not the Python web API.
export type RunStatus = "queued" | "running" | "completed" | "cancelled" | "failed" | "interrupted";
export type Run = { id: string; session_id: string; request_id: string; status: RunStatus };
export type Session = { id: string; title: string; updated_at_ms: number };
export type Bootstrap = {
  version: string; protocol_version: number; token: string; workspace: string;
  backend: "demo" | "nagents"; provider: string; model: string | null; read_only: boolean;
};
export type Call = { call_id: string; name: string; arguments: unknown };
export type Approval = {
  id: string; run_id: string; session_id: string; root_session_id: string;
  agent_title: string; depth: number; call: Call; preview: string;
};
export type Snapshot = { runs: Run[]; approvals: Approval[] };
export type Task = {
  run_id: string; session_id: string; parent_run_id: string; parent_session_id: string;
  root_run_id: string; root_session_id: string; call_id: string; title: string;
  depth: number; read_only: boolean; status: RunStatus;
};
export type TaskLimits = { max_concurrent: number; max_executions: number; max_depth: number };
export type Queue = { session_id: string; paused: boolean; entries: { run: Run; prompt: string }[] };
export type QueueAction = "pause" | "resume" | "clear";
export type SteeringStatus = "pending" | "offered" | "applied" | "uncertain" | "undelivered";
export type SteeringReceipt = {
  input_id: string; run_id: string; session_id: string; request_id: string;
  position: number; prompt: string; status: SteeringStatus;
};
export type SteeringState = {
  run_id: string; session_id: string; run_status: RunStatus; supported: boolean; accepting: boolean; receipts: SteeringReceipt[];
};

export type Event = {
  schema_version: number; sequence: number; run_id: string; session_id: string; timestamp_ms: number;
} & (
  | { event: "run_started" | "run_queued"; prompt: string }
  | { event: "queue_updated"; paused: boolean }
  | { event: "steering_updated"; input_id: string }
  | { event: "text_delta" | "text_completed" | "notice"; text: string }
  | { event: "tool_started"; call: Call }
  | { event: "tool_completed"; result: { call_id: string; name: string; result: unknown; error: string }; duration_ms: number }
  | { event: "tool_approval_timing"; call_id: string; elapsed_ms: number; waiting: boolean }
  | { event: "approval_requested"; approval_id: string; call: Call; preview: string }
  | { event: "approval_resolved"; approval_id: string; allowed: boolean }
  | { event: "task_started" | "task_updated"; task: Task }
  | { event: "task_finished"; task: Task; error: string }
  | { event: "run_finished"; status: RunStatus; error: string }
);

export const sessionPath = (session: string) => `/api/sessions/${encodeURIComponent(session)}`;
export const runPath = (run: string) => `/api/runs/${encodeURIComponent(run)}`;

export function eventsUrl(location: Pick<Location, "protocol" | "host">, session: string, after: number) {
  const query = new URLSearchParams({ session_id: session, after: String(after) });
  return `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/events?${query}`;
}

export function acceptEvent(value: unknown, session: string, after: number): value is Event {
  if (!value || typeof value !== "object") return false;
  const record = value as Partial<Event>;
  return record.schema_version === 1 && record.session_id === session && typeof record.run_id === "string"
    && typeof record.event === "string" && typeof record.sequence === "number"
    && Number.isSafeInteger(record.sequence) && record.sequence > after;
}

export interface Transport {
  request<T>(path: string, token?: string, body?: object, signal?: AbortSignal): Promise<T>;
  watch(session: string, token: string, receive: (event: Event) => void, connected: (open: boolean) => void, invalid: () => void): () => void;
}

export const browserTransport: Transport = {
  async request<T>(path: string, token?: string, body?: object, signal?: AbortSignal): Promise<T> {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: {
        ...(token && { "x-ngn-token": token }),
        ...(body !== undefined && { "content-type": "application/json" }),
      },
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      signal,
    });
    const value: unknown = await response.json().catch(() => undefined);
    if (!response.ok || value === undefined) {
      const detail = value && typeof value === "object" && "detail" in value ? value.detail : undefined;
      throw new Error(typeof detail === "string" ? detail : `ngn returned HTTP ${response.status}`);
    }
    return value as T;
  },
  watch(session, token, receive, connected, invalid) {
    let stopped = false, after = 0;
    let socket: WebSocket, retry: ReturnType<typeof setTimeout>;
    function connect() {
      if (stopped) return;
      socket = new WebSocket(eventsUrl(window.location, session, after), ["ngn.events.v1", `ngn.token.${token}`]);
      socket.onopen = () => { if (!stopped) connected(true); };
      socket.onmessage = ({ data }) => {
        if (stopped) return;
        try {
          const record: unknown = JSON.parse(data);
          if (!acceptEvent(record, session, after)) return;
          receive(record);
          after = record.sequence;
        } catch { invalid(); }
      };
      socket.onclose = () => {
        if (stopped) return;
        connected(false);
        retry = setTimeout(connect, 1200);
      };
    }
    connect();
    return () => {
      stopped = true;
      clearTimeout(retry);
      socket?.close();
    };
  },
};
