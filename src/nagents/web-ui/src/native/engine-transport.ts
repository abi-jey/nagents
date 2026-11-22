// Project version-one wire messages onto the shared native workspace views.
// The only credential here is the same-origin gateway's browser token.
import type { Event, Queue, Run, Session, SteeringReceipt, SteeringState, Task, Transport } from "./transport.js";

type EngineQueue = {
  session_id: string; paused: boolean; limit: number;
  entries: { request_id: string; prompt: string; run_id: string | null }[];
};

const tags = new Map<string, Event["event"]>([
  ["run.queued", "run_queued"], ["run.started", "run_started"], ["run.finished", "run_finished"],
  ["queue.updated", "queue_updated"], ["steering.updated", "steering_updated"],
  ["text.delta", "text_delta"], ["text.completed", "text_completed"], ["notice", "notice"],
  ["tool.started", "tool_started"], ["tool.completed", "tool_completed"], ["tool.approval_timing", "tool_approval_timing"],
  ["approval.requested", "approval_requested"], ["approval.resolved", "approval_resolved"],
  ["task.started", "task_started"], ["task.updated", "task_updated"], ["task.finished", "task_finished"],
  ["session.updated", "session_updated"],
]);

export function engineRequest(request: Transport["request"]): Transport["request"] {
  return async <T>(path: string, token?: string, body?: object, signal?: AbortSignal) => {
    const call = <R>(endpoint: string, input = body) => request<R>(`/api/v1${endpoint}`, token, input, signal);
    const endpoint = path.slice("/api".length);
    let result: unknown;
    if (endpoint === "/sessions") {
      result = body ? { session: await call<Session>(endpoint) } : { sessions: await call<Session[]>(endpoint) };
    } else if (/^\/sessions\/[^/]+\/queue$/.test(endpoint)) {
      const engine = await call<EngineQueue>(endpoint);
      const queue: Queue = {
        session_id: engine.session_id, paused: engine.paused,
        entries: engine.entries.map(({ request_id, prompt, run_id }) => run_id
          ? { prompt, run: { id: run_id, request_id, session_id: engine.session_id, status: "queued" } }
          : { prompt, request_id }),
      };
      result = { queue, limit: engine.limit };
    } else if (/^\/sessions\/[^/]+\/runs$/.test(endpoint)) {
      const { warning, ...run } = await call<Pick<Run, "session_id" | "request_id" | "status"> & { warning?: string }>(
        endpoint, { ...body, interactive: true },
      );
      result = { run, warning };
    } else if (/^\/sessions\/[^/]+\/tasks$/.test(endpoint)) {
      result = { tasks: await call<Task[]>(endpoint) };
    } else if (/^\/runs\/[^/]+\/steer$/.test(endpoint)) {
      result = body ? { receipt: await call<SteeringReceipt>(endpoint) } : { steering: await call<SteeringState>(endpoint) };
    } else if (endpoint === "/approvals") {
      const decision = body as { approval_id?: string; call_id?: string; run_id?: string; allow?: boolean } | undefined;
      if (!decision?.approval_id || !decision.call_id) throw new Error("Approval no longer identifies an exact tool call");
      const { approval_id, ...input } = decision;
      result = await call(`/approvals/${encodeURIComponent(approval_id)}`, input);
    } else {
      result = await call(endpoint);
    }
    return result as T;
  };
}

export function readEngineEvent(raw: unknown, session: string, after: number, includeChildren = false): Event | undefined {
  if (!raw || typeof raw !== "object") return;
  const { event, ...envelope } = raw as Partial<Omit<Event, "event">> & { event?: { type?: string } };
  const scoped = envelope.session_id === session ||
    (includeChildren && typeof envelope.session_id === "string" && /^child-[a-f0-9]{32}$/.test(envelope.session_id));
  if (envelope.schema_version !== 1 || !scoped || typeof envelope.run_id !== "string" ||
      typeof envelope.sequence !== "number" || !Number.isSafeInteger(envelope.sequence) || envelope.sequence <= after ||
      !event || typeof event.type !== "string") return;
  // Envelope identity wins over any similarly named field in an additive
  // engine payload. Unknown event names have no action in the projection.
  const { type, ...payload } = event;
  return { ...payload, ...envelope, event: tags.get(type) ?? "unknown" } as Event;
}
