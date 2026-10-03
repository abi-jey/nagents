import { request } from "../../api/client.js";
import type { DelegationText, LiveCapability, LiveCreated, LiveDelegation, LiveDelegationDetails, LiveDelegationRecord, LiveSnapshot } from "./types.js";

export async function capability(token: string, signal?: AbortSignal): Promise<LiveCapability> {
  return (await request("live", token, undefined, signal)).json() as Promise<LiveCapability>;
}

export async function delegationDetails(token: string, delegation: LiveDelegation, signal: AbortSignal): Promise<LiveDelegationDetails> {
  const data: LiveDelegationDetails = await (await request(`live/sessions/${encodeURIComponent(delegation.sessionId)}/delegations/${encodeURIComponent(delegation.id)}`, token, undefined, signal)).json();
  if (!data || data.voice_session_id !== delegation.sessionId || data.chat_session_id !== delegation.chatSessionId || data.delegation_id !== delegation.id || data.source !== "app_callback" || (delegation.runId && data.run_id !== delegation.runId))
    throw new Error("These details belong to a different voice request. Refresh and try again.");
  function text(value: unknown, limit: number): value is DelegationText {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    const item = value as Record<string, unknown>;
    if (typeof item.text !== "string" || typeof item.truncated !== "boolean" || typeof item.characters !== "number" || !Number.isSafeInteger(item.characters)) return false;
    const size = Array.from(item.text).length;
    return size <= limit && item.characters >= size && (item.truncated || item.characters === size);
  }
  const event = (item: LiveDelegationRecord) => !!item && typeof item === "object" && item.voice_session_id === data.voice_session_id && item.chat_session_id === data.chat_session_id && item.delegation_id === data.delegation_id &&
    Number.isSafeInteger(item.seq) && item.seq > 0 && item.seq <= data.seq && ["queued", "working", "completed", "failed", "cancelled"].includes(item.status) &&
    [item.agent, item.provider, item.model, item.text, item.run_id].every(value => typeof value === "string") &&
    (!(item.status === "working" || item.status === "completed") || !!item.run_id.trim()) && (!item.run_id || item.run_id === data.run_id);
  if (!Number.isSafeInteger(data.seq) || data.seq < delegation.seq || !event(data) || !data.request || typeof data.request !== "object" || Array.isArray(data.request) ||
      (data.request.transcript !== undefined && !text(data.request.transcript, 32768)) || (data.request.input !== undefined && !text(data.request.input, 32768)) ||
      (data.result !== undefined && (!text(data.result, 16384) || !["assistant_output", "terminal_explanation"].includes(data.result.kind))) ||
      !Array.isArray(data.timeline) || data.timeline.length > 8 || !data.timeline.every(event) || typeof data.timeline_truncated !== "boolean")
    throw new Error("The delegation details were incomplete or inconsistent. Refresh and try again.");
  return data;
}

export function liveApi(token: string) {
  return {
    create: async (voice: string, signal: AbortSignal, revision: string, sessionId: string, sdp?: string): Promise<LiveCreated> =>
      (await request("live/sessions", token, { voice, revision, session_id: sessionId, ...(sdp ? { sdp } : {}) }, signal)).json() as Promise<LiveCreated>,
    read: async (id: string, after: number, signal: AbortSignal): Promise<LiveSnapshot> =>
      (await request(`live/sessions/${encodeURIComponent(id)}?after=${after}`, token, undefined, signal)).json() as Promise<LiveSnapshot>,
    close: async (id: string): Promise<LiveSnapshot> =>
      (await request(`live/sessions/${encodeURIComponent(id)}/close`, token, {}, AbortSignal.timeout(45_000))).json() as Promise<LiveSnapshot>,
  };
}
