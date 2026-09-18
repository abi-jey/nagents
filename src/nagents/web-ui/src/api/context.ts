import { request } from "./client.js";
import type { ContextStats } from "../types";

export function validContextStats(data: unknown): data is ContextStats {
  if (!data || typeof data !== "object") return false;
  const value = data as Record<string, unknown>;
  if (typeof value.total_tokens !== "number" || !Array.isArray(value.components)) return false;
  return value.components.every((component) => {
    if (!component || typeof component !== "object") return false;
    const item = component as Record<string, unknown>;
    return (
      typeof item.key === "string" &&
      typeof item.label === "string" &&
      typeof item.tokens === "number"
    );
  });
}

export async function readContextStats(
  token: string,
  sessionId: string,
  signal: AbortSignal,
): Promise<ContextStats> {
  const response = await request(
    `sessions/${encodeURIComponent(sessionId)}/context`,
    token,
    undefined,
    signal,
  );
  const data: unknown = await response.json();
  if (!validContextStats(data)) {
    throw new Error("Invalid context statistics. Reconnect to try again.");
  }
  return data;
}
