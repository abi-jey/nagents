import { requestBinary } from "../../api/client.js";
import type { DictationContext } from "./types.js";

export async function transcribe(
  context: DictationContext,
  audio: Blob,
  signal: AbortSignal,
): Promise<string> {
  signal.throwIfAborted();
  if (!context.token || !context.sessionId || !context.config.revision)
    throw new Error("Reconnect and refresh settings before recording.");
  if (audio.type !== "audio/wav" || audio.size <= 44 || audio.size > context.config.max_bytes)
    throw new Error("Recording is empty or exceeds the WAV upload limit.");
  const data: unknown = await (await requestBinary(
    "dictation/transcribe", context.token, audio, {
      "X-Ngn-Session": context.sessionId,
      "X-Ngn-Settings-Revision": context.config.revision,
    }, signal,
  )).json();
  signal.throwIfAborted();
  if (!data || typeof data !== "object" || !("text" in data) || typeof data.text !== "string")
    throw new SyntaxError("The server returned an invalid transcription. Your draft is kept.");
  if (!data.text.trim()) throw new Error("No speech was transcribed. Try again or type your message.");
  return data.text;
}
