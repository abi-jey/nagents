import type { Entry } from "./transcript.js";

function summaryText(value: string): string {
  const text = value.replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, " ").replace(/\s+/g, " ").trim();
  return text.length > 120 ? `${text.slice(0, 119)}…` : text;
}

export function executionTarget(entry: Entry): string {
  if (entry.kind !== "tool" || !entry.inputs) return "";
  let value: unknown;
  try { value = JSON.parse(entry.inputs); } catch { return ""; }
  if (!value || typeof value !== "object" || Array.isArray(value)) return "";
  const inputs = value as Record<string, unknown>;
  if (entry.title === "channel_send") {
    return summaryText([inputs.channel, inputs.destination].filter((item): item is string => typeof item === "string").join(" · "));
  }
  for (const key of ["command", "cmd", "file_path", "path", "url", "pattern", "query", "task_id"]) {
    if (typeof inputs[key] !== "string") continue;
    // Summaries are plain, single-line text; preserve the original in Inputs.
    const text = summaryText(inputs[key]);
    if (text) return text;
  }
  return "";
}

export function executionName(name: string): string {
  const names: Record<string, string> = {
    channel_send: "Send to channel", read_file: "Read file", write_file: "Write file",
    edit_file: "Edit file", run_shell_command: "Run command", shell: "Run command",
    list_directory: "List directory", search_files: "Search files",
  };
  return Object.hasOwn(names, name) ? names[name] : name;
}

export function executionStatus(entry: Entry): { label: string; tone: string } {
  const saved = entry.state === "Recorded result";
  if (entry.error) return { label: saved ? "Saved error" : "Error", tone: "error" };
  if (saved) return { label: "Saved result", tone: "neutral" };
  const state = entry.state || "Recorded activity";
  if (state === "Waiting for approval") return { label: "Needs approval", tone: "pending" };
  if (["Error", "Failed", "Interrupted"].includes(state)) return { label: state, tone: "error" };
  if (["Requested", "Running", "Receiving output"].includes(state))
    return { label: state === "Receiving output" ? "Running" : state, tone: "active" };
  if (state === "Completed") return {
    label: ["delegate", "schedule_wakeup", "wake_up_in"].includes(entry.title || "") ? "Request completed" : "Completed",
    tone: "complete",
  };
  const pending = ["Waiting for approval", "Approval denied", "Awaiting execution result", "No result recorded", "Disconnected", "Cancelled"].includes(state);
  return { label: state === "Awaiting execution result" ? "Awaiting result" : state, tone: pending ? "pending" : "neutral" };
}

export function executionDuration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${(ms / 60000).toFixed(1)} min`;
}
