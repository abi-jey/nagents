import type { SettingsProfile, SettingsValues } from "./types.js";

export type SettingsDraft = { [Key in keyof SettingsValues]: string };
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

export function createDraft(values: SettingsValues): SettingsDraft {
  return {
    model: values.model,
    agent: values.agent,
    shell_timeout: String(values.shell_timeout),
    max_output: String(values.max_output),
    max_file_bytes: String(values.max_file_bytes),
    max_tool_rounds: String(values.max_tool_rounds),
    max_subagent_depth: String(values.max_subagent_depth),
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
    shell_timeout: 0,
    max_output: 0,
    max_file_bytes: 0,
    max_tool_rounds: 0,
    max_subagent_depth: 0,
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
  for (const limit of executionLimits) {
    const raw = draft[limit.key].trim();
    const value = Number(raw);
    const timeout = limit.key === "shell_timeout";
    const validNumber = timeout
      ? /^(?:\d+(?:\.\d*)?|\.\d+)$/.test(raw) && Number.isFinite(value)
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
