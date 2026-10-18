import type { IconName } from "../../components/Icon.js";
import type { ActivityCategory, ChatAppearance } from "../settings/appearance.js";
import { toolPresentation } from "./toolPresentation.js";
import type { ResponseMetadata } from "./responseMetadata.js";
import type { Entry, TranscriptItem } from "./transcript.js";

export const activityLabels: Record<Exclude<ActivityCategory, "reasoning">, { label: string; icon: IconName }> = {
  shell: { label: "Commands", icon: "terminal" }, files: { label: "File changes", icon: "edit" },
  agents: { label: "Agent activity", icon: "team" }, notices: { label: "Notices", icon: "clock" },
  tools: { label: "Tools", icon: "tools" },
};
export function activityCategory(item: TranscriptItem): Exclude<ActivityCategory, "reasoning"> | undefined {
  if (item.kind !== "entry") return "agents";
  const entry = item.entry;
  if (entry.delivery || entry.liveCaption || entry.uploads?.length) return undefined;
  if (entry.kind === "tool") {
    const family = toolPresentation(entry.title || "").family;
    return family === "command" ? "shell" : ["edit", "write"].includes(family) ? "files"
      : family === "team" ? "agents" : ["schedule", "compact"].includes(family) ? "notices" : "tools";
  }
  if (entry.kind === "task") return "agents";
  if (["context", "status", "notification", "wakeup", "agent_change", "retained_tasks"].includes(entry.kind)) return "notices";
  return undefined;
}
export function importantActivity(item: TranscriptItem): boolean {
  return item.kind === "entry" ? !!item.entry.error || item.entry.kind === "error" ||
    ["error", "warning"].includes(item.entry.level || "") ||
    ["Error", "Failed", "Waiting for approval", "Approval required", "Approval denied", "Interrupted", "No result recorded"].includes(item.entry.state || "")
    : item.kind === "thread" && ["failed", "waiting"].includes(item.thread.state.toLowerCase());
}
function scope(item: TranscriptItem): string {
  return item.kind === "entry" ? `${item.entry.runId || ""}:${item.entry.taskId || ""}:${item.entry.activation || 0}` : "agents";
}
export function itemKey(item: TranscriptItem): string {
  return item.kind === "entry" ? item.entry.id : item.kind === "thread" ? `task:${item.thread.id}` : item.id;
}
export type ActivityGroup = { kind: "activity_group"; id: string; category: Exclude<ActivityCategory, "reasoning">; scope: string; items: TranscriptItem[] };
export type PresentationItem = TranscriptItem | ActivityGroup | { kind: "run_footer"; id: string; metadata: ResponseMetadata };

/** Derive reading layout without changing transcript identities or execution state. */
export function activityLayout(items: TranscriptItem[], appearance?: ChatAppearance, insideGroup = false): PresentationItem[] {
  const footers = new Map<string, { index: number; metadata: ResponseMetadata; taskId: string }>();
  const atIndex = new Map<number, PresentationItem[]>();
  if (!insideGroup && appearance?.runUsage !== "hidden") {
    items.forEach((item, index) => {
      if (item.kind !== "entry" || !item.entry.responseMetadata?.run_id) return;
      const metadata = item.entry.responseMetadata;
      const id = `run-usage:${metadata.response_session_id}:${metadata.run_id}`;
      footers.set(id, { index, metadata, taskId: item.entry.taskId || "" });
    });
    // Include subsequent activity proven to belong to the same run. A task's
    // private transcript gets its own footer instead of inflating the root run.
    const lastActivity = new Map<string, number>();
    items.forEach((item, index) => {
      if (item.kind !== "entry" || !item.entry.runId) return;
      lastActivity.set(`${item.entry.runId}:${item.entry.taskId || ""}`, index);
    });
    for (const [id, footer] of footers) {
      const index = Math.max(footer.index, lastActivity.get(`${footer.metadata.run_id}:${footer.taskId}`) ?? -1);
      if ((footer.metadata.run_usage?.response_count || 0) <= 1 || !footer.metadata.run_usage?.usage_response_count) continue;
      const existing = atIndex.get(index) || [];
      existing.push({ kind: "run_footer", id, metadata: footer.metadata });
      atIndex.set(index, existing);
    }
  }
  const result: PresentationItem[] = [];
  items.forEach((item, index) => {
    const category = activityCategory(item), rule = category && appearance?.activity[category];
    const hidden = rule?.placement === "hidden" && !importantActivity(item);
    if (!hidden) {
      if (category && rule?.placement === "grouped" && !insideGroup) {
        const previous = result.at(-1), itemScope = scope(item);
        if (previous?.kind === "activity_group" && previous.category === category && previous.scope === itemScope) previous.items.push(item);
        else result.push({ kind: "activity_group", id: `activity:${itemKey(item)}`, category, scope: itemScope, items: [item] });
      } else result.push(item);
    }
    result.push(...(atIndex.get(index) || []));
  });
  return result;
}
export function activityState(items: TranscriptItem[]): string {
  if (items.some(importantActivity)) return "Needs attention";
  const entries = items.filter((item): item is { kind: "entry"; entry: Entry } => item.kind === "entry");
  if (entries.some(({ entry }) => entry.streaming || ["Preparing", "Running", "Starting", "Requested", "Receiving output", "Awaiting execution result"].includes(entry.state || "")) ||
    items.some(item => item.kind === "thread" && ["running", "starting", "queued"].includes(item.thread.state.toLowerCase()))) return "Running";
  if (items.length && items.every(item => item.kind === "entry" ? item.entry.state === "Completed" :
    item.kind === "thread" && ["completed", "done", "returned"].includes(item.thread.state.toLowerCase()))) return "Done";
  return "";
}
