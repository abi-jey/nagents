import { request } from "../../api/client.js";
import { readVoiceContext } from "./context.js";
import { providerDelegationId } from "./identifiers.js";
import type { DelegationText, LiveCapability, LiveCreated, LiveDelegation, LiveDelegationDetails, LiveDelegationTimelineEvent, LiveSnapshot, VoiceContext, VoiceContextDetailsRecord } from "./types.js";

function boundedText(value: unknown, limit: number): value is DelegationText {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const item = value as Record<string, unknown>;
  if (typeof item.text !== "string" || typeof item.truncated !== "boolean" || typeof item.characters !== "number" || !Number.isSafeInteger(item.characters) ||
      (item.characters_complete !== undefined && typeof item.characters_complete !== "boolean") || (item.characters_complete === false && !item.truncated)) return false;
  const size = Array.from(item.text).length;
  return size <= limit && item.characters >= size && (item.truncated || item.characters === size);
}

const identifier = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{32}$/.test(value);
const round = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 && value <= 1_000_000;

export async function capability(token: string, signal?: AbortSignal): Promise<LiveCapability> {
  return (await request("live", token, undefined, signal)).json() as Promise<LiveCapability>;
}

export async function delegationDetails(token: string, delegation: LiveDelegation, signal: AbortSignal): Promise<LiveDelegationDetails> {
  if (!providerDelegationId(delegation.id)) throw new Error("The voice request identifier is invalid. Refresh and try again.");
  const data: LiveDelegationDetails = await (await request(`live/sessions/${encodeURIComponent(delegation.sessionId)}/delegation-details?delegation_id=${encodeURIComponent(delegation.id)}`, token, undefined, signal)).json();
  if (!data || data.voice_session_id !== delegation.sessionId || data.chat_session_id !== delegation.chatSessionId || data.delegation_id !== delegation.id || data.source !== "app_callback" || (delegation.runId && data.run_id !== delegation.runId))
    throw new Error("These details belong to a different voice request. Refresh and try again.");
  const event = (item: LiveDelegationTimelineEvent) => !!item && typeof item === "object" && item.voice_session_id === data.voice_session_id && item.chat_session_id === data.chat_session_id && item.delegation_id === data.delegation_id &&
    Number.isSafeInteger(item.seq) && item.seq > 0 && item.seq <= data.seq && ["queued", "working", "completed", "failed", "cancelled"].includes(item.status) &&
    [item.agent, item.provider, item.model, item.text, item.run_id].every(value => typeof value === "string") &&
    (!(item.status === "working" || item.status === "completed") || !!item.run_id.trim()) && (!item.run_id || item.run_id === data.run_id) &&
    (item.type === undefined || ["delegation", "model_context", "http_request_body"].includes(item.type)) &&
    (!(item.type === "model_context" || item.type === "http_request_body") || (identifier(item.model_call_id) && round(item.round))) &&
    (item.attempt_id === undefined || identifier(item.attempt_id)) &&
    (item.type !== "http_request_body" || (identifier(item.attempt_id) && item.segmented === true)) &&
    (item.capture_limited === undefined || item.capture_limited === true) &&
    (item.detail_type === undefined || ["delegation", "model_context", "http_request_body", "live_append", "live_delivery_failed"].includes(item.detail_type));
  if (!Number.isSafeInteger(data.seq) || data.seq < delegation.seq || !event(data) || !data.request || typeof data.request !== "object" || Array.isArray(data.request) ||
      (data.request.transcript !== undefined && !boundedText(data.request.transcript, 32768)) || (data.request.input !== undefined && !boundedText(data.request.input, 32768)) ||
      (data.result !== undefined && (!boundedText(data.result, 16384) || !["assistant_output", "terminal_explanation"].includes(data.result.kind))) ||
      !Array.isArray(data.timeline) || data.timeline.length > 96 || !data.timeline.every(event) || typeof data.timeline_truncated !== "boolean" ||
      (data.model_requests_truncated !== undefined && typeof data.model_requests_truncated !== "boolean") ||
      (data.model_requests !== undefined && (!Array.isArray(data.model_requests) || data.model_requests.length > 32 || !data.model_requests.every(item =>
        !!item && typeof item === "object" && Number.isSafeInteger(item.seq) && item.seq > 0 && item.seq <= data.seq &&
        ["model_context", "http_request_body"].includes(item.type) && identifier(item.model_call_id) && round(item.round) &&
        (item.attempt_id === undefined || identifier(item.attempt_id)) &&
        (item.type !== "http_request_body" || (item.segmented === true && identifier(item.attempt_id))) && boundedText(item.payload, 65536) &&
        new TextEncoder().encode(item.payload.text).length <= 65536) ||
        data.model_requests.reduce((total, item) => total + new TextEncoder().encode(item.payload.text).length, 0) > 262144)))
    throw new Error("The delegation details were incomplete or inconsistent. Refresh and try again.");
  if ((data.live_updates_truncated !== undefined && typeof data.live_updates_truncated !== "boolean") ||
      (data.live_updates !== undefined && (!Array.isArray(data.live_updates) || data.live_updates.length > 96 || !data.live_updates.every(item =>
        !!item && typeof item === "object" && Number.isSafeInteger(item.seq) && item.seq > 0 && item.seq <= data.seq &&
        ["thinking", "commentary", "instructions"].includes(item.kind) &&
        (item.outcome === "failed" ? item.wire_type === "" : item.outcome === "sent" &&
          ["delegation.context.append", "session.context.append", `session.${item.kind}.append`].includes(item.wire_type)) &&
        boundedText(item.content, 2000) && new TextEncoder().encode(item.content.text).length <= 2000) ||
        data.live_updates.reduce((total, item) => total + new TextEncoder().encode(item.content.text).length, 0) > 192000)))
    throw new Error("The Live update details were incomplete or inconsistent. Refresh and try again.");
  const liveUpdates = data.live_updates || [];
  if (new Set(liveUpdates.map(item => item.seq)).size !== liveUpdates.length || liveUpdates.some(item => {
    const metadata = data.timeline.find(event => event.seq === item.seq);
    return data.model_requests?.some(capture => capture.seq === item.seq) || metadata &&
      ((metadata.type !== undefined && metadata.type !== "delegation") || metadata.detail_type !== (item.outcome === "sent" ? "live_append" : "live_delivery_failed"));
  })) throw new Error("The Live updates did not match their events. Refresh and try again.");
  const captures = data.model_requests || [];
  if (new Set(captures.map(item => item.seq)).size !== captures.length || captures.some(item => {
    const metadata = data.timeline.find(event => event.seq === item.seq);
    return metadata && (metadata.type !== item.type || metadata.model_call_id !== item.model_call_id || metadata.round !== item.round || metadata.attempt_id !== item.attempt_id || metadata.capture_limited);
  })) throw new Error("The captured model requests did not match their events. Refresh and try again.");
  return data;
}

export async function voiceContextDetails(token: string, sessionId: string, context: VoiceContext, signal: AbortSignal): Promise<VoiceContextDetailsRecord> {
  const data: VoiceContextDetailsRecord = await (await request(`live/sessions/${encodeURIComponent(sessionId)}/context`, token, undefined, signal)).json();
  if (!data || data.voice_session_id !== sessionId || data.chat_session_id !== context.chat_session_id || data.fingerprint !== context.fingerprint)
    throw new Error("These details belong to a different voice session. Refresh and try again.");
  if (!readVoiceContext(data, context.chat_session_id) || typeof data.available !== "boolean" || typeof data.reason !== "string" || data.reason.length > 512 ||
      !boundedText(data.instructions, 16384) || new TextEncoder().encode(data.instructions.text).length > 16384 ||
      typeof data.history_truncated !== "boolean" || !Array.isArray(data.history) || data.history.length > 16 ||
      !data.history.every(item => !!item && typeof item === "object" && item.type === "message" && ["user", "assistant"].includes(item.role) &&
        Array.isArray(item.content) && item.content.length <= 16 && item.content.every(part => !!part && typeof part === "object" &&
          ["input_text", "output_text"].includes(part.type) && typeof part.text === "string")) ||
      data.history.reduce((total, item) => total + new TextEncoder().encode(JSON.stringify(item)).length, 0) > 32768)
    throw new Error("The voice context details were incomplete or inconsistent. Refresh and try again.");
  return data;
}

export function liveApi(token: string) {
  return {
    create: async (voice: string, signal: AbortSignal, revision: string, sessionId: string): Promise<LiveCreated> =>
      (await request("live/sessions", token, { voice, revision, session_id: sessionId }, signal)).json() as Promise<LiveCreated>,
    read: async (id: string, after: number, signal: AbortSignal): Promise<LiveSnapshot> =>
      (await request(`live/sessions/${encodeURIComponent(id)}?after=${after}`, token, undefined, signal)).json() as Promise<LiveSnapshot>,
    close: async (id: string): Promise<LiveSnapshot> =>
      (await request(`live/sessions/${encodeURIComponent(id)}/close`, token, {}, AbortSignal.timeout(45_000))).json() as Promise<LiveSnapshot>,
  };
}
