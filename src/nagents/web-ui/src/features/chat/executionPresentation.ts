import type { Entry } from "./transcript.js";

export function executionTarget(entry: Entry): string {
  if (entry.kind !== "tool" || !entry.inputs) return "";
  let value: unknown;
  try { value = JSON.parse(entry.inputs); } catch { return ""; }
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const inputs = value as Record<string, unknown>;
  for (const key of ["command", "cmd", "file_path", "path", "url", "pattern", "query", "task_id"]) {
    if (typeof inputs[key] !== "string") continue;
    // Summaries are plain, single-line text; preserve the original in Inputs.
    const text = inputs[key].replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, " ").replace(/\s+/g, " ").trim();
    if (text) return text.length > 120 ? `${text.slice(0, 119)}…` : text;
  }
  return "";
}

export function executionDuration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${(ms / 60000).toFixed(1)} min`;
}
