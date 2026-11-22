import type { Entry } from "../features/chat/transcript.js";
import type { Approval as DialogApproval } from "../types.js";
import type { TeamAgent } from "../features/team/model.js";
import type { Approval, Bootstrap, Event, Task } from "./transport.js";

export function isArchive(session: string) { return /^(?:python|host)-archive-[a-f0-9]{64}$/.test(session); }

const json = (value: unknown) => JSON.stringify(value, null, 2);
type RunTools = { calls: Map<string, string>; approvals: Map<string, string> };

/** Project the durable Rust stream onto the existing React conversation records. */
export class NativeTranscript {
  private cursor = 0;
  private entries = new Map<string, Entry>();
  private responses = new Map<string, string>();
  private tools = new Map<string, RunTools>();
  private started = new Set<string>();

  constructor(private session: string, private run?: string) {}

  snapshot(): Entry[] { return [...this.entries.values()]; }

  apply(record: Event): boolean {
    if (record.session_id !== this.session || (this.run && record.run_id !== this.run) || record.sequence <= this.cursor) return false;
    this.cursor = record.sequence;
    const runId = record.run_id, id = `native:${record.sequence}`;
    const tools = this.tools.get(runId);
    const add = (entry: Omit<Entry, "id" | "runId">) => {
      this.entries.set(id, { ...entry, id, runId });
      if (this.entries.size > 1000) this.entries.delete(this.entries.keys().next().value!);
      return true;
    };
    const update = (key: string | undefined, change: Partial<Entry>) => {
      const entry = key && this.entries.get(key);
      if (!entry) return false;
      this.entries.set(entry.id, { ...entry, ...change });
      return true;
    };
    switch (record.event) {
      case "run_started":
        this.started.add(runId);
        return add({ kind: "user", text: record.prompt });
      case "text_delta":
      case "text_completed": {
        const key = this.responses.get(runId);
        const complete = record.event === "text_completed";
        if (complete) this.responses.delete(runId);
        else if (!key || !this.entries.has(key)) this.responses.set(runId, id);
        const previous = key ? this.entries.get(key)?.text || "" : "";
        const text = complete ? record.text : previous + record.text;
        return update(key, { text, streaming: !complete }) ||
          add({ kind: "assistant", text, streaming: !complete });
      }
      case "tool_started": {
        this.responses.delete(runId);
        const run = tools || { calls: new Map<string, string>(), approvals: new Map<string, string>() };
        run.calls.set(record.call.call_id, id);
        this.tools.set(runId, run);
        return add({ kind: "tool", title: record.call.name, callId: record.call.call_id, text: "",
          state: "Running", inputs: json(record.call.arguments) });
      }
      case "tool_approval_timing":
        return update(tools?.calls.get(record.call_id), {
          state: record.waiting ? "Waiting for approval" : "Running", durationMs: record.elapsed_ms,
        });
      case "approval_requested": {
        const key = tools?.calls.get(record.call.call_id);
        if (!key) return false;
        tools?.approvals.set(record.approval_id, key);
        return update(key, { state: "Waiting for approval", approvalId: record.approval_id });
      }
      case "approval_resolved": {
        const key = tools?.approvals.get(record.approval_id);
        tools?.approvals.delete(record.approval_id);
        return update(key, { state: record.allowed ? "Running" : "Approval denied", approval: record.allowed ? "Allowed once" : "Denied" });
      }
      case "tool_completed": {
        const key = tools?.calls.get(record.result.call_id);
        tools?.calls.delete(record.result.call_id);
        const denied = key && this.entries.get(key)?.approval === "Denied";
        return update(key, { state: denied ? "Approval denied" : record.result.error ? "Failed" : "Completed",
          error: record.result.error, result: json(record.result.result), durationMs: record.duration_ms });
      }
      case "notice": {
        // The Rust importer emits only labelled display text, never live events.
        if (isArchive(this.session)) {
          const message = /^(user|assistant)\n([\s\S]*)$/.exec(record.text);
          if (message) return add({ kind: message[1] === "user" ? "user" : "assistant", text: message[2] });
        }
        return add({ kind: "status", text: record.text });
      }
      case "task_started": return add({ kind: "status", text: `${record.task.title} started` });
      case "task_finished": return add({ kind: record.error ? "error" : "status",
        text: `${record.task.title}: ${record.task.status}${record.error ? ` — ${record.error}` : ""}` });
      case "run_finished": {
        const response = this.responses.get(runId);
        this.responses.delete(runId);
        let changed = update(response, { streaming: false });
        // Recovery records the interruption without replaying tools or
        // manufacturing tool results. Mark only this run's unfinished cards.
        const state = record.status === "cancelled" ? "Cancelled" :
          record.status === "completed" ? "No result recorded" : "Interrupted";
        for (const key of tools?.calls.values() || []) {
          if (this.entries.get(key)?.approval !== "Denied") changed = update(key, { state }) || changed;
        }
        this.tools.delete(runId);
        const started = this.started.delete(runId);
        if (!started || record.status === "completed") return changed;
        const text = record.status === "cancelled" ? "Stopped" :
          record.status === "interrupted" ? "Interrupted before the turn finished" : "Run failed";
        return add({ kind: record.error && record.status === "failed" ? "error" : "status",
          text: `${text}${record.error ? ` — ${record.error}` : ""}` });
      }
      default: return false;
    }
  }
}

export function dialogApproval(approval?: Approval): DialogApproval | undefined {
  if (!approval) return undefined;
  return { run_id: approval.run_id, approval_id: approval.id, call_id: approval.call.call_id, tool: approval.call.name,
    description: `This call was requested by ${approval.agent_title || "Assistant"}.`, preview: approval.preview,
    arguments: approval.call.arguments, session_id: approval.session_id, title: approval.agent_title,
    depth: approval.depth, activation: 1 };
}

export function teamAgents(session: string, config: Bootstrap | undefined, tasks: readonly Task[], running: boolean, approval?: Approval): TeamAgent[] {
  const common = { profile: "", prompt: "", provider: config?.provider || "", providerKind: config?.provider || "",
    model: config?.model || "", startedAt: 0, updatedAt: 0, canMessage: false, messageReason: "" };
  const waiting = approval?.session_id === session;
  return [{ ...common, id: "", parentId: "", name: "Assistant",
    phase: waiting ? "waiting" : running ? "running" : "ready", reason: waiting ? "Waiting for approval" : "" },
  ...tasks.filter((task) => task.root_session_id === session).map((task): TeamAgent => {
    const waiting = approval?.session_id === task.session_id;
    return { ...common, id: task.session_id, parentId: task.parent_session_id === session ? "" : task.parent_session_id,
      name: task.title, phase: waiting ? "waiting" : task.status === "completed" ? "done" :
        task.status === "cancelled" ? "cancelled" : task.status === "failed" || task.status === "interrupted" ? "failed" : "running",
      reason: waiting ? "Waiting for approval" : task.read_only ? "Read-only agent" : "" };
  })];
}
