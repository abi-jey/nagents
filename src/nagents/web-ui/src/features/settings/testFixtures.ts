import type { SettingsValues } from "./types.js";

/** Complete settings payload; individual tests specify only meaningful differences. */
export function settingsValues(overrides: Partial<SettingsValues> = {}): SettingsValues {
  return {
    model: "startup-model", agent: "assistant", provider: "openai", base_url: "", api: "auto", auth: "auto", api_key_env: "OPENAI_API_KEY",
    shell_timeout: 30, max_output: 16384, max_file_bytes: 1048576, max_tool_rounds: 100, max_subagent_depth: 2,
    dictation_enabled: false, dictation_model: "gpt-4o-mini-transcribe", dictation_language: "", dictation_max_seconds: 60,
    compact_trigger: "auto", compact_tokens: 200000, compact_messages: 100, submit_mode: "queue", read_only: false, ...overrides,
  };
}
