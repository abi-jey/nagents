import { request } from "./client.js";
import type { ActivityReply } from "../types.js";

export async function pollActivity({
  token,
  sessionId,
  cursor,
  signal,
  receive,
  failed,
  interval = 1000,
}: {
  token: string;
  sessionId: string;
  cursor: number;
  signal: AbortSignal;
  receive: (reply: ActivityReply) => void;
  failed: (message: string) => void;
  interval?: number;
}): Promise<void> {
  while (!signal.aborted) {
    try {
      const response = await request(
        `activity/${encodeURIComponent(sessionId)}/${cursor}`,
        token,
        undefined,
        signal,
      );
      const data: unknown = await response.json();
      if (signal.aborted) return;
      if (
        !data ||
        typeof data !== "object" ||
        !("session_id" in data) ||
        data.session_id !== sessionId ||
        !("cursor" in data) ||
        typeof data.cursor !== "number" ||
        !Number.isSafeInteger(data.cursor) ||
        data.cursor < 0 ||
        (data.cursor < cursor &&
          !("truncated" in data && data.truncated === true)) ||
        !("events" in data) ||
        !Array.isArray(data.events) ||
        !data.events.every(
          (event: unknown) =>
            !!event &&
            typeof event === "object" &&
            "event" in event &&
            typeof event.event === "string" &&
            (!("session_id" in event) || event.session_id === sessionId),
        ) ||
        !("active_run_id" in data) ||
        typeof data.active_run_id !== "string" ||
        !("truncated" in data) ||
        typeof data.truncated !== "boolean" ||
        !("pending_wakeups" in data) ||
        !Array.isArray(data.pending_wakeups) ||
        !data.pending_wakeups.every(
          (wakeup: unknown) =>
            !!wakeup &&
            typeof wakeup === "object" &&
            ["wakeup_id", "task_id", "due_at", "reason"].every(
              (key) =>
                key in wakeup &&
                typeof (wakeup as Record<string, unknown>)[key] === "string",
            ),
        )
      ) {
        throw new Error(
          "Invalid background activity response. Activity was not applied.",
        );
      }
      const reply = data as ActivityReply;
      // Cursor ownership, not text equality, prevents repeated batches from replaying output.
      receive({
        ...reply,
        events: reply.cursor !== cursor ? reply.events : [],
        truncated: reply.cursor !== cursor && reply.truncated,
      });
      cursor = reply.cursor;
    } catch (cause) {
      if (signal.aborted) return;
      failed(
        cause instanceof Error
          ? cause.message
          : "Background activity unavailable. Retrying read-only polling.",
      );
    }
    if (signal.aborted) return;
    await new Promise<void>((resolve) => {
      const finish = () => {
        clearTimeout(timer);
        signal.removeEventListener("abort", finish);
        resolve();
      };
      const timer = setTimeout(finish, interval);
      signal.addEventListener("abort", finish, { once: true });
    });
  }
}
