import type { SettingsProfile, SettingsValues } from "./types.js";

export type SettingsDraft = {
  [Key in keyof SettingsValues]: SettingsValues[Key] extends boolean ? boolean : string;
};
export type DraftErrors = Partial<Record<keyof SettingsValues, string>>;

export const executionLimits = [
  {
    key: "shell_timeout",
    label: "Shell timeout",
    unit: "seconds",
    min: 0,
    max: 600,
    help: "Per shell command. More than 0, up to 600 seconds.",
  },
  {
    key: "max_output",
    label: "Tool output limit",
    unit: "bytes",
    min: 1024,
    max: 1048576,
    help: "1,024 to 1,048,576 bytes. Tool output, not model tokens.",
  },
  {
    key: "max_file_bytes",
    label: "File read limit",
    unit: "bytes",
    min: 1024,
    max: 4194304,
    help: "1,024 to 4,194,304 bytes per file read.",
  },
  {
    key: "max_tool_rounds",
    label: "Tool rounds",
    unit: "rounds",
    min: 1,
    max: 1000,
    help: "1 to 1,000 tool rounds per run.",
  },
  {
    key: "max_subagent_depth",
    label: "Subagent depth",
    unit: "levels",
    min: 0,
    max: 8,
    help: "0 to 8 nested levels. 0 prevents delegation.",
  },
] as const;

export const providerApis = ["auto", "chat_completions", "responses", "messages"] as const;
export const providerAuths = ["auto", "api-key", "chatgpt"] as const;

export const compactionTriggers = ["auto", "tokens", "messages", "off"] as const;

export const compactionLimits = [
  {
    key: "compact_tokens",
    label: "Token window",
    unit: "tokens",
    min: 1024,
    max: 10000000,
    help: "1,024 to 10,000,000 tokens of context. Compaction runs near 70% of this window.",
  },
  {
    key: "compact_messages",
    label: "Message count",
    unit: "messages",
    min: 1,
    max: 10000,
    help: "1 to 10,000 messages in the conversation before compacting.",
  },
] as const;

export function createDraft(values: SettingsValues): SettingsDraft {
  return {
    model: values.model,
    agent: values.agent,
    provider: values.provider,
    base_url: values.base_url,
    api: values.api,
    auth: values.auth,
    api_key_env: values.api_key_env,
    shell_timeout: String(values.shell_timeout),
    max_output: String(values.max_output),
    max_file_bytes: String(values.max_file_bytes),
    max_tool_rounds: String(values.max_tool_rounds),
    max_subagent_depth: String(values.max_subagent_depth),
    dictation_enabled: values.dictation_enabled,
    dictation_model: values.dictation_model,
    dictation_language: values.dictation_language,
    dictation_max_seconds: String(values.dictation_max_seconds),
    compact_trigger: values.compact_trigger,
    compact_tokens: String(values.compact_tokens),
    compact_messages: String(values.compact_messages),
    submit_mode: values.submit_mode || "queue",
    read_only: values.read_only ?? false,
  };
}

export function selectProfile(
  draft: SettingsDraft,
  name: string,
  profiles: SettingsProfile[],
): SettingsDraft {
  const profile = profiles.find((candidate) => candidate.name === name);
  if (!profile) return draft;
  return { ...draft, agent: name, model: profile.model || draft.model };
}

export function parseDraft(
  draft: SettingsDraft,
  profiles: SettingsProfile[],
): { ok: true; values: SettingsValues } | { ok: false; errors: DraftErrors } {
  const errors: DraftErrors = {};
  const values: SettingsValues = {
    model: draft.model.trim(),
    agent: draft.agent,
    provider: draft.provider.trim(),
    base_url: draft.base_url.trim(),
    api: draft.api,
    auth: draft.auth,
    api_key_env: draft.api_key_env.trim(),
    shell_timeout: 0,
    max_output: 0,
    max_file_bytes: 0,
    max_tool_rounds: 0,
    max_subagent_depth: 0,
    dictation_enabled: draft.dictation_enabled,
    dictation_model: draft.dictation_model.trim(),
    dictation_language: draft.dictation_language,
    dictation_max_seconds: Number(draft.dictation_max_seconds.trim()),
    compact_trigger: draft.compact_trigger,
    compact_tokens: 0,
    compact_messages: 0,
    submit_mode: draft.submit_mode || "queue",
    read_only: draft.read_only,
  };
  if (
    !values.model ||
    [...values.model].length > 200 ||
    /[\u0000-\u001f\u007f-\u009f]/.test(draft.model)
  ) {
    errors.model =
      "Enter a model ID of 1 to 200 characters, without control characters.";
  }
  if (!profiles.some((profile) => profile.name === draft.agent)) {
    errors.agent = "Choose a profile provided by this server.";
  }
  if (
    !values.provider ||
    [...values.provider].length > 40 ||
    /[\u0000-\u001f\u007f-\u009f]/.test(draft.provider)
  ) {
    errors.provider = "Choose a provider provided by this server.";
  }
  if ([...values.base_url].length > 300 || /[\u0000-\u001f\u007f-\u009f]/.test(draft.base_url)) {
    errors.base_url = "Enter an HTTP(S) endpoint of at most 300 printable characters, or leave blank.";
  }
  if (!(providerApis as readonly string[]).includes(values.api)) {
    errors.api = "Choose a supported API mode.";
  }
  if (!(providerAuths as readonly string[]).includes(values.auth)) {
    errors.auth = "Choose a supported authentication mode.";
  }
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(values.api_key_env) || [...values.api_key_env].length > 64) {
    errors.api_key_env = "Enter an environment variable name, not the key itself.";
  }
  if (typeof draft.dictation_enabled !== "boolean")
    errors.dictation_enabled = "Choose whether dictation is enabled.";
  if (!values.dictation_model || [...values.dictation_model].length > 200 ||
      /[\p{C}\p{Z}]/u.test(draft.dictation_model.replaceAll(" ", "")))
    errors.dictation_model = "Enter a transcription model ID of 1 to 200 printable characters.";
  if (!/^(?:[a-z]{2})?$/.test(draft.dictation_language))
    errors.dictation_language = "Use two lowercase language letters, such as en, or leave blank for automatic detection.";
  if (!/^\d+$/.test(draft.dictation_max_seconds.trim()) ||
      !Number.isSafeInteger(values.dictation_max_seconds) ||
      values.dictation_max_seconds < 1 || values.dictation_max_seconds > 300)
    errors.dictation_max_seconds = "Enter a whole number from 1 to 300 seconds. The administrator's ceiling also applies.";
  if (!(compactionTriggers as readonly string[]).includes(values.compact_trigger))
    errors.compact_trigger = "Choose a supported compaction trigger.";
  if (!["queue", "interrupt"].includes(values.submit_mode)) errors.submit_mode = "Choose Queue or Interrupt.";
  if (typeof values.read_only !== "boolean") errors.read_only = "Choose whether this workspace is read-only.";
  for (const limit of [...executionLimits, ...compactionLimits]) {
    const raw = draft[limit.key].trim();
    const value = Number(raw);
    const timeout = limit.key === "shell_timeout";
    const validNumber = timeout
      ? /^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(raw) && Number.isFinite(value)
      : /^\d+$/.test(raw) && Number.isSafeInteger(value);
    if (
      !validNumber ||
      value > limit.max ||
      (timeout ? value <= limit.min : value < limit.min)
    ) {
      errors[limit.key] = timeout
        ? "Enter a number greater than 0 and at most 600 seconds."
        : `Enter a whole number from ${limit.min.toLocaleString("en-US")} to ${limit.max.toLocaleString("en-US")} ${limit.unit}.`;
    }
    values[limit.key] = value;
  }
  return Object.keys(errors).length ? { ok: false, errors } : { ok: true, values };
}
