import type { Entry } from "./transcript.js";
import { toolObject } from "./toolResultPresentation.js";
import { toolPresentation } from "./toolPresentation.js";

function summaryText(value: string): string {
  const text = value.replace(/[\u0000-\u001f\u007f-\u009f\u202a-\u202e\u2066-\u2069]/g, " ").replace(/\s+/g, " ").trim();
  return text.length > 120 ? `${text.slice(0, 119)}…` : text;
}

export function executionTarget(entry: Entry): string {
  if (entry.kind !== "tool" || !entry.inputs) return "";
  const inputs = toolObject(entry.inputs);
  if (!inputs) return "";
  if (entry.title === "channel_send") {
    return summaryText([inputs.channel, inputs.destination].filter((item): item is string => typeof item === "string").join(" · "));
  }
  const family = toolPresentation(entry.title || "").family;
  const keys = family === "schedule" ? ["reason"] : family === "skill" ? ["name"]
    : family === "search" || entry.title === "find" ? ["query", "pattern", "path"]
    : ["command", "cmd", "file_path", "path", "url", "pattern", "query", "session_id"];
  for (const key of keys) {
    if (typeof inputs[key] !== "string") continue;
    // Summaries are plain, single-line text; preserve the original in Inputs.
    const text = summaryText(inputs[key]);
    if (text) return text;
  }
  return "";
}

export function executionName(name: string): string {
  return toolPresentation(name).label;
}

export function executionStatus(entry: Entry): { label: string; tone: string } {
  const saved = entry.state === "Recorded result";
  if (entry.error) return { label: saved ? "Saved error" : "Error", tone: "error" };
  if (saved) return { label: "Saved result", tone: "neutral" };
  if (entry.kind === "tool" && entry.title === "shell" && entry.state === "Completed" && entry.commandOutcome) {
    if (entry.commandOutcome.kind === "timed_out") return { label: "Timed out", tone: "error" };
    if (entry.commandOutcome.exitCode !== 0) return { label: `Exit ${entry.commandOutcome.exitCode}`, tone: "error" };
  }
  const state = entry.state || "Recorded activity";
  if (state === "Waiting for approval") return { label: "Needs approval", tone: "pending" };
  if (["Error", "Failed", "Interrupted"].includes(state)) return { label: state, tone: "error" };
  if (["Preparing", "Starting", "Requested", "Running", "Receiving output", "Awaiting execution result"].includes(state))
    return { label: state === "Receiving output" ? "Running" : state === "Awaiting execution result" ? "Awaiting result" : state, tone: "active" };
  if (state === "Completed") return {
    label: ["to_agent", "schedule_wakeup", "wake_up_in"].includes(entry.title || "") ? "Request completed" : "Completed",
    tone: "complete",
  };
  const pending = ["Waiting for approval", "Approval denied", "Awaiting execution result", "No result recorded", "Disconnected", "Cancelled"].includes(state);
  return { label: state === "Awaiting execution result" ? "Awaiting result" : state, tone: pending ? "pending" : "neutral" };
}

export function commandOutcomeDescription(entry: Entry): string {
  if (entry.kind !== "tool" || entry.title !== "shell" || entry.error || entry.state !== "Completed" || !entry.commandOutcome) return "";
  const outcome = entry.commandOutcome;
  return outcome.kind === "timed_out"
    ? `Command timed out${outcome.exitCode === undefined ? "" : ` (exit code ${outcome.exitCode})`}`
    : `Command exited with code ${outcome.exitCode}`;
}

export function executionDuration(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s` : `${(ms / 60000).toFixed(1)} min`;
}
