import type { VoiceContext } from "./types.js";

export function readVoiceContext(value: unknown, root: string): VoiceContext | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return;
  const item = value as Record<string, unknown>;
  if (!["recent", "summary", "none"].includes(String(item.mode)) ||
      !["recent", "existing_summary", "generated_summary", "recent_fallback", "none"].includes(String(item.method)) ||
      (item.chat_session_id !== root && !(item.mode === "none" && item.chat_session_id === "")) ||
      typeof item.fingerprint !== "string" || !/^([a-f0-9]{64})?$/.test(item.fingerprint) ||
      typeof item.summary_included !== "boolean" || typeof item.omitted_content !== "boolean" ||
      typeof item.notice !== "string" || item.notice.length > 512) return;
  for (const [field, limit] of [["message_count", 14], ["characters", 7000], ["bytes", 7000], ["omitted_messages", Number.MAX_SAFE_INTEGER]] as const) {
    const number = item[field];
    if (typeof number !== "number" || !Number.isSafeInteger(number) || number < 0 || number > limit) return;
  }
  if (Number(item.characters) > Number(item.bytes)) return;
  return item as unknown as VoiceContext;
}

export function contextLabel(context: VoiceContext): string {
  if (context.method === "none") return "Fresh voice";
  if (context.method === "generated_summary") return "Chat brief";
  if (!context.message_count) return "No saved context";
  return "Chat context";
}
