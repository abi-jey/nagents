import { preview, text } from "../../api/events.js";
import type { Snapshot, WireEvent } from "../../types.js";

export type Entry = {
  kind: "user" | "assistant" | "tool" | "task" | "status" | "error";
  text: string;
  title?: string;
  callId?: string;
  streaming?: boolean;
  inputs?: string;
  result?: string;
  error?: string;
  state?: string;
  durationMs?: number;
  runId?: string;
  taskId?: string;
  parentTaskId?: string;
  depth?: number;
  followup?: number;
  approvalId?: string;
  approval?: string;
};

export function appendEvent(entries: Entry[], event: WireEvent): Entry[] {
  const runId = text(event, "run_id");
  if (event.event === "run_finished" || event.event === "client_disconnected") {
    return entries.map((entry) => {
      if (entry.streaming) return { ...entry, streaming: false };
      if (
        ![
          "Requested",
          "Running",
          "Receiving output",
          "Waiting for approval",
          "Follow-up requested",
        ].includes(entry.state || "")
      )
        return entry;
      if (runId && entry.runId !== runId) return entry;
      const state =
        event.event === "client_disconnected"
          ? "Disconnected"
          : event.status === "cancelled"
            ? "Cancelled"
            : event.status === "failed"
              ? "Interrupted"
              : event.status === "completed"
                ? "No result recorded"
                : entry.state;
      return { ...entry, state };
    });
  }
  const streaming = entries.findLastIndex(
    (entry) => entry.kind === "assistant" && entry.streaming,
  );
  if (event.event === "text_chunk") {
    const chunk = text(event, "chunk");
    return streaming >= 0
      ? entries.map((entry, index) =>
          index === streaming ? { ...entry, text: entry.text + chunk } : entry,
        )
      : [...entries, { kind: "assistant", text: chunk, streaming: true }];
  }
  if (event.event === "text_done") {
    return streaming >= 0
      ? entries.map((entry, index) =>
          index === streaming
            ? {
                ...entry,
                text: text(event, "text") || entry.text,
                streaming: false,
              }
            : entry,
        )
      : [...entries, { kind: "assistant", text: text(event, "text") }];
  }
  if (event.event === "tool_call") {
    return [
      ...entries,
      {
        kind: "tool",
        title: text(event, "name"),
        callId: text(event, "id"),
        inputs: preview(event.arguments),
        text: "",
        state: "Requested",
        runId,
      },
    ];
  }
  if (event.event === "tool_output" || event.event === "tool_result") {
    const id = text(event, "call_id") || text(event, "id");
    const index = entries.findLastIndex(
      (entry) =>
        entry.kind === "tool" && entry.callId === id && entry.runId === runId,
    );
    const update =
      event.event === "tool_output"
        ? {
            text: (index >= 0 ? entries[index].text : "") + text(event, "text"),
            state: "Receiving output",
          }
        : {
            result: preview(event.result),
            error: text(event, "error"),
            state: event.saved
              ? "Recorded result"
              : event.error
                ? "Error"
                : "Completed",
            durationMs:
              typeof event.duration_ms === "number"
                ? event.duration_ms
                : undefined,
          };
    if (index >= 0) {
      return entries.map((entry, i) =>
        i === index
          ? {
              ...entry,
              title: text(event, "name") || entry.title,
              ...update,
            }
          : entry,
      );
    }
    return [
      ...entries,
      {
        kind: "tool",
        title: text(event, "name") || text(event, "tool"),
        text: "",
        callId: id,
        runId,
        ...update,
      },
    ];
  }
  if (event.event === "error")
    return [...entries, { kind: "error", text: text(event, "message") }];
  if (event.event === "notice")
    return [...entries, { kind: "status", text: text(event, "text") }];
  if (event.event === "approval") {
    const index = entries.findLastIndex(
      (entry) =>
        entry.kind === "tool" &&
        entry.callId === event.id &&
        entry.runId === runId,
    );
    return entries.map((entry, i) =>
      i === index
        ? {
            ...entry,
            state: "Waiting for approval",
            approvalId: text(event, "approval_id"),
            taskId: text(event, "task_id"),
          }
        : entry,
    );
  }
  if (event.event === "approval_closed") {
    return entries.map((entry) =>
      entry.approvalId === event.approval_id
        ? {
            ...entry,
            state: "Requested",
            approval:
              event.decision === "allow"
                ? "Allowed once"
                : event.expired
                  ? "Expired; denied"
                  : "Denied",
          }
        : entry,
    );
  }
  if (
    event.event === "task_started" ||
    event.event === "task_completed" ||
    event.event === "task_message"
  ) {
    const taskId = text(event, "task_id");
    const followup = typeof event.followup === "number" ? event.followup : 0;
    const index = entries.findLastIndex(
      (entry) =>
        entry.kind === "task" &&
        entry.taskId === taskId &&
        entry.followup === followup &&
        entry.runId === runId,
    );
    const entry: Entry =
      index >= 0
        ? { ...entries[index] }
        : {
            kind: "task",
            title: text(event, "name"),
            text: "",
            taskId,
            followup,
            runId,
            parentTaskId: text(event, "parent_task_id"),
            depth: typeof event.depth === "number" ? event.depth : undefined,
          };
    if (event.event === "task_completed") {
      entry.result = text(event, "result");
      entry.error = text(event, "error");
      entry.state =
        event.status === "cancelled"
          ? "Cancelled"
          : entry.error || event.status === "failed"
            ? "Error"
            : "Completed";
    } else {
      entry.inputs = text(event, "prompt");
      entry.state =
        event.event === "task_started" ? "Running" : "Follow-up requested";
    }
    return index < 0
      ? [...entries, entry]
      : entries.map((previous, i) => (i === index ? entry : previous));
  }
  if (
    event.event === "compaction_started" ||
    event.event === "compaction_done" ||
    event.event === "rate_limit"
  ) {
    return [
      ...entries,
      {
        kind: "status",
        text:
          event.event === "compaction_started"
            ? "Compacting context"
            : event.event === "compaction_done"
              ? "Context compacted"
              : "Provider rate limit; waiting for its retry",
      },
    ];
  }
  return entries;
}

export function fromHistory(messages: Snapshot["history"]): Entry[] {
  let entries: Entry[] = [];
  for (const message of messages) {
    if (message.role === "tool") {
      entries = appendEvent(entries, {
        event: "tool_result",
        id: message.tool_call_id,
        name: message.name,
        result: message.content,
        saved: true,
      });
      continue;
    }
    if (message.content)
      entries.push({
        kind: message.role === "user" ? "user" : "assistant",
        text: message.content,
      });
    for (const call of message.tool_calls)
      entries = appendEvent(entries, {
        event: "tool_call",
        id: call.id,
        name: call.name,
        arguments: call.arguments,
      });
  }
  return entries.map((entry) =>
    entry.state === "Requested"
      ? { ...entry, state: "No result recorded" }
      : entry,
  );
}
