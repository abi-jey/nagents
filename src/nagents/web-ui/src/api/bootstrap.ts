import { request } from "./client.js";
import type { Bootstrap } from "../types.js";

function object(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function validBootstrap(value: unknown): value is Bootstrap {
  if (!object(value) || typeof value.token !== "string" || !/^[A-Za-z0-9_-]{1,256}$/.test(value.token) ||
      !["workspace", "provider", "model", "agent", "active_run_id"].every((key) => typeof value[key] === "string") ||
      typeof value.demo !== "boolean" || (value.active_session_id !== undefined && typeof value.active_session_id !== "string") ||
      (value.active_run_background !== undefined && typeof value.active_run_background !== "boolean")) return false;
  const config = value.dictation;
  return object(config) && ["enabled", "available", "admin_enabled"].every((key) => typeof config[key] === "boolean") &&
    ["status", "api_key_env", "revision"].every((key) => typeof config[key] === "string") &&
    ["max_seconds", "max_bytes"].every((key) => typeof config[key] === "number" && Number.isFinite(config[key])) &&
    config.sample_rate === 16000 && config.channels === 1 && config.sample_width === 2 && config.content_type === "audio/wav";
}

// Bootstrap remains the existing same-origin, unauthenticated credential source.
// Never echo its token, response body, or an upstream exception into UI feedback.
export async function readBootstrap(signal: AbortSignal): Promise<Bootstrap> {
  try {
    signal.throwIfAborted();
    const response = await request("bootstrap", "", undefined, signal);
    const value: unknown = await response.json();
    signal.throwIfAborted();
    if (!validBootstrap(value)) throw new Error();
    return value;
  } catch {
    if (signal.aborted) throw new DOMException("Credential refresh cancelled.", "AbortError");
    throw new Error("Local connection credentials could not be refreshed. Reconnection will retry.");
  }
}
