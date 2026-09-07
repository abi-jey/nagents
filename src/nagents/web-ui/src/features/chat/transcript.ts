import { preview, text } from "../../api/events.js";
import type { Snapshot, WireEvent } from "../../types.js";

export type Entry = {
  kind: "user" | "assistant" | "tool" | "status" | "error";
  text: string;
  title?: string;
  callId?: string;
  streaming?: boolean;
};

export function appendEvent(entries: Entry[], event: WireEvent): Entry[] {
  if (event.event === "run_finished") {
    return entries.map((entry) =>
      entry.streaming ? { ...entry, streaming: false } : entry,
    );
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
        text: preview(event.arguments),
      },
    ];
  }
  if (event.event === "tool_output" || event.event === "tool_result") {
    const id = text(event, "call_id") || text(event, "id");
    const index = entries.findLastIndex(
      (entry) => entry.kind === "tool" && entry.callId === id,
    );
    const output =
      event.event === "tool_output"
        ? text(event, "text")
        : preview(event.error || event.result);
    if (index >= 0) {
      return entries.map((entry, i) =>
        i === index
          ? {
              ...entry,
              title: text(event, "name") || entry.title,
              text: entry.text + "\n" + output,
            }
          : entry,
      );
    }
    return [
      ...entries,
      {
        kind: "tool",
        title: text(event, "name") || text(event, "tool"),
        text: output,
        callId: id,
      },
    ];
  }
  if (event.event === "error")
    return [...entries, { kind: "error", text: text(event, "message") }];
  if (event.event === "notice")
    return [...entries, { kind: "status", text: text(event, "text") }];
  if (
    event.event === "task_started" ||
    event.event === "task_completed" ||
    event.event === "task_message"
  ) {
    return [
      ...entries,
      {
        kind: "tool",
        title: `${text(event, "name")} / ${event.event.replace("task_", "")}`,
        text:
          text(event, "result") ||
          text(event, "prompt") ||
          text(event, "error"),
      },
    ];
  }
  if (
    event.event === "compaction_started" ||
    event.event === "compaction_done" ||
    event.event === "rate_limit"
  ) {
    return [
      ...entries,
      { kind: "status", text: event.event.replaceAll("_", " ") },
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
  return entries;
}
