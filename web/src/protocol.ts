export type WireEvent = { event: string; [key: string]: unknown };
export type Entry = {
  kind: "user" | "assistant" | "tool" | "status" | "error";
  text: string;
  title?: string;
  callId?: string;
  streaming?: boolean;
};

export function text(event: WireEvent, key: string): string {
  const value = event[key];
  return typeof value === "string" ? value : "";
}

export function preview(value: unknown): string {
  return value == null
    ? ""
    : typeof value === "string"
      ? value
      : JSON.stringify(value, null, 2);
}

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

export async function readEvents(
  body: ReadableStream<Uint8Array>,
  receive: (event: WireEvent) => void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let newline: number;
      while ((newline = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (!line.trim()) continue;
        const event: unknown = JSON.parse(line);
        if (
          !event ||
          typeof event !== "object" ||
          !("event" in event) ||
          typeof event.event !== "string"
        ) {
          throw new Error(
            "Invalid event received. Reconnect without resubmitting the prompt.",
          );
        }
        receive(event as WireEvent);
      }
      if (buffer.length > 8 * 1024 * 1024)
        throw new Error("Stream event exceeded the client limit.");
      if (done) break;
    }
    if (buffer.trim())
      throw new Error("The stream ended partway through an event.");
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
