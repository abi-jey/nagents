import { request } from "./client.js";
import type { ContextStats } from "../types";

export function validContextStats(data: unknown): data is ContextStats {
  if (!data || typeof data !== "object") return false;
  const value = data as Record<string, unknown>;
  const count = (input: unknown) => typeof input === "number" && Number.isSafeInteger(input) && input >= 0;
  if (!count(value.total_tokens) || !Array.isArray(value.components)) return false;
  if (value.context_window !== null && (!count(value.context_window) || value.context_window === 0)) return false;
  if (!["configured", "provider", "heuristic"].includes(String(value.context_window_source))) return false;
  if (value.observed_prompt_tokens !== null && !count(value.observed_prompt_tokens)) return false;
  if (value.observed_completion_tokens !== null && !count(value.observed_completion_tokens)) return false;
  if (value.compaction !== undefined) {
    if (!value.compaction || typeof value.compaction !== "object" || Array.isArray(value.compaction)) return false;
    const policy = value.compaction as Record<string, unknown>;
    if (typeof policy.trigger !== "string") return false;
    for (const field of ["token_threshold", "token_budget", "message_threshold"]) {
      if (policy[field] !== undefined && (!count(policy[field]) || policy[field] === 0)) return false;
    }
  }
  return value.components.every((component) => {
    if (!component || typeof component !== "object") return false;
    const item = component as Record<string, unknown>;
    return (
      typeof item.key === "string" &&
      typeof item.label === "string" &&
      count(item.tokens)
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
