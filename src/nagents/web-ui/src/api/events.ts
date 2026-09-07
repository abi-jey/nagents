import type { WireEvent } from "../types.js";

export function text(event: WireEvent, key: string): string {
  const value = event[key];
  return typeof value === "string" ? value : "";
}

export function preview(value: unknown): string {
  return value === undefined
    ? ""
    : typeof value === "string"
      ? value
      : JSON.stringify(value, null, 2);
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
