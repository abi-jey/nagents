export type ResponseUsage = {
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  prompt_reported: boolean;
  completion_reported: boolean;
  total_reported: boolean;
  reasoning_tokens: number;
  reasoning_reported: boolean;
  reasoning_in_output: "included" | "separate" | "unknown";
};
export type RunUsage = ResponseUsage & {
  response_count: number;
  usage_response_count: number;
  reasoning_response_count: number;
  prompt_response_count: number;
  completion_response_count: number;
  total_response_count: number;
  complete: boolean;
};

export type ResponseMetadata = {
  response_id: string;
  response_session_id: string;
  run_id?: string;
  revision: number;
  round_number: number;
  history_id: string;
  status: "thinking" | "responding" | "completed" | "cancelled" | "failed";
  summary: string;
  summary_truncated: boolean;
  summary_expired: boolean;
  usage_reported: boolean;
  usage?: ResponseUsage;
  run_usage?: RunUsage;
};

const record = (value: unknown): value is Record<string, unknown> =>
  !!value && typeof value === "object" && !Array.isArray(value);
const count = (value: unknown): value is number => typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

function reportedUsage(raw: unknown): ResponseUsage | undefined {
  return record(raw) && [raw.prompt_tokens, raw.completion_tokens, raw.total_tokens, raw.reasoning_tokens].every(count) &&
    typeof raw.reasoning_reported === "boolean" && typeof raw.reasoning_in_output === "string" &&
    ["included", "separate", "unknown"].includes(raw.reasoning_in_output)
    ? { prompt_tokens: raw.prompt_tokens as number, completion_tokens: raw.completion_tokens as number,
      total_tokens: raw.total_tokens as number, reasoning_tokens: raw.reasoning_tokens as number,
      prompt_reported: raw.prompt_reported === true || raw.prompt_reported === undefined && Number(raw.prompt_tokens) > 0,
      completion_reported: raw.completion_reported === true || raw.completion_reported === undefined && Number(raw.completion_tokens) > 0,
      total_reported: raw.total_reported === true || raw.total_reported === undefined && Number(raw.total_tokens) > 0,
      reasoning_reported: raw.reasoning_reported, reasoning_in_output: raw.reasoning_in_output as ResponseUsage["reasoning_in_output"] }
    : undefined;
}

// Only the host's bounded, displayable summary is accepted. Provider protocol
// items, signatures and raw reasoning chunks never enter the presentation model.
export function responseMetadata(value: unknown): ResponseMetadata | undefined {
  if (!record(value) || typeof value.response_id !== "string" || !value.response_id ||
      typeof value.response_session_id !== "string" || !value.response_session_id ||
      !count(value.revision) || value.revision < 1 || !count(value.round_number) ||
      typeof value.history_id !== "string" || typeof value.summary !== "string" || typeof value.status !== "string" ||
      !["thinking", "responding", "completed", "cancelled", "failed"].includes(value.status)) return undefined;
  const usage = reportedUsage(value.usage);
  const rawRun = value.run_usage, runCounts = reportedUsage(rawRun);
  const runUsage: RunUsage | undefined = runCounts && record(rawRun) && count(rawRun.response_count) &&
    count(rawRun.usage_response_count) && count(rawRun.reasoning_response_count) && typeof rawRun.complete === "boolean" &&
    count(rawRun.prompt_response_count) && count(rawRun.completion_response_count) && count(rawRun.total_response_count) &&
    [rawRun.usage_response_count, rawRun.reasoning_response_count, rawRun.prompt_response_count,
      rawRun.completion_response_count, rawRun.total_response_count].every(coverage => coverage <= Number(rawRun.response_count))
    ? { ...runCounts, response_count: rawRun.response_count, usage_response_count: rawRun.usage_response_count,
      reasoning_response_count: rawRun.reasoning_response_count,
      prompt_response_count: rawRun.prompt_response_count, completion_response_count: rawRun.completion_response_count,
      total_response_count: rawRun.total_response_count,
      prompt_reported: rawRun.prompt_response_count > 0, completion_reported: rawRun.completion_response_count > 0,
      total_reported: rawRun.total_response_count > 0,
      reasoning_reported: runCounts.reasoning_reported && rawRun.reasoning_response_count === rawRun.response_count,
      complete: rawRun.complete && rawRun.prompt_response_count === rawRun.response_count && rawRun.completion_response_count === rawRun.response_count } : undefined;
  const summary = Array.from(value.summary).slice(0, 16384).join("");
  return {
    response_id: value.response_id, response_session_id: value.response_session_id,
    ...(typeof value.run_id === "string" && value.run_id ? { run_id: value.run_id } : {}),
    revision: value.revision, round_number: value.round_number, history_id: value.history_id,
    status: value.status as ResponseMetadata["status"], summary,
    summary_truncated: value.summary_truncated === true || summary.length < value.summary.length,
    summary_expired: value.summary_expired === true, usage_reported: value.usage_reported === true,
    ...(usage ? { usage } : {}),
    ...(runUsage ? { run_usage: runUsage } : {}),
  };
}

export function sameResponse(left: ResponseMetadata, right: ResponseMetadata): boolean {
  return left.response_id === right.response_id && left.response_session_id === right.response_session_id;
}

export function latestResponseMetadata(saved?: ResponseMetadata, previous?: ResponseMetadata): ResponseMetadata | undefined {
  if (!saved || !previous) return saved || previous;
  if (!sameResponse(saved, previous)) return saved;
  return saved.revision >= previous.revision ? saved : previous;
}

export function hasResponseDetails(value?: ResponseMetadata, includeRunUsage = false): boolean {
  return !!value && (!!value.summary || value.status === "thinking" || value.usage_reported && hasVisibleUsage(value.usage) || value.summary_expired ||
    includeRunUsage && !!value.run_id && (value.run_usage?.response_count || 0) > 1 && (value.run_usage?.usage_response_count || 0) > 0);
}

export function hasVisibleUsage(usage?: ResponseUsage): boolean {
  return !!usage && (usage.prompt_reported || usage.completion_reported || usage.reasoning_reported);
}
