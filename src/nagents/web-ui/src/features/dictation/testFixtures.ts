import type { DictationConfig, DictationContext } from "./types.js";

export const dictationConfig: DictationConfig = {
  enabled: true,
  available: true,
  admin_enabled: true,
  status: "Ready for OpenAI API-key transcription.",
  api_key_env: "OPENAI_API_KEY",
  max_seconds: 1,
  max_bytes: 36096,
  sample_rate: 16000,
  channels: 1,
  sample_width: 2,
  content_type: "audio/wav",
  revision: 'opaque/revision:01+"not-a-counter"',
};
export const dictationContext: DictationContext = {
  token: "synthetic-test-token",
  sessionId: "root-test-session",
  config: dictationConfig,
};

export function deferred<Value>() {
  let resolve!: (value: Value) => void;
  let reject!: (cause: unknown) => void;
  const promise = new Promise<Value>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
