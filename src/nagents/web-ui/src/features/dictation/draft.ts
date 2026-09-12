export type DraftInsertion = { ok: true; prompt: string } | { ok: false; error: string };

export function promptFailure(prompt: string, sessionId: string): string {
  // Match the textarea's UTF-16 limit as well as the actual JSON wire byte limit.
  if (prompt.length > 32000)
    return "The combined draft exceeds 32,000 characters. Shorten the draft or transcription; both are kept.";
  if (new TextEncoder().encode(JSON.stringify({ session_id: sessionId, prompt })).byteLength > 65536)
    return "The message exceeds the 64 KiB request limit. Shorten the draft or transcription; both are kept.";
  return "";
}

export function insertDraft(current: string, text: string, sessionId: string): DraftInsertion {
  if (!text.trim()) return { ok: false, error: "Enter some transcription text before inserting." };
  const separator = current && !/\s$/.test(current) && !/^\s/.test(text) ? "\n" : "";
  const prompt = current + separator + text;
  const error = promptFailure(prompt, sessionId);
  return error ? { ok: false, error } : { ok: true, prompt };
}
