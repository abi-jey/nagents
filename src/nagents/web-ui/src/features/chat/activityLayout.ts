import type { IconName } from "../../components/Icon.js";
import type { ActivityCategory, ChatAppearance } from "../settings/appearance.js";
import { toolPresentation } from "./toolPresentation.js";
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
export type PresentationItem = TranscriptItem | ActivityGroup;

/** Keep the first displayed slot and parent of each live record. */
export class ActivityLayout {
  private appearance = "";
  private slots = new Map<string, { group?: string }>();

  render(items: TranscriptItem[], appearance?: ChatAppearance, insideGroup = false): PresentationItem[] {
    const signature = JSON.stringify([appearance, insideGroup]);
    if (signature !== this.appearance) { this.appearance = signature; this.slots.clear(); }
    if (!items.length) this.slots.clear();
    const result: PresentationItem[] = [], groups = new Map<string, ActivityGroup>();
    for (const item of items) {
      const key = itemKey(item), category = activityCategory(item), rule = category && appearance?.activity[category];
      let slot = this.slots.get(key);
      if (!slot) {
        if (rule?.placement === "hidden" && !importantActivity(item)) continue;
        if (category && rule?.placement === "grouped" && !insideGroup) {
          const previous = result.at(-1), itemScope = scope(item);
          // A streamed name can change category. Give that call its own stable
          // group rather than moving it between parents as its name arrives.
          const pending = item.kind === "entry" && item.entry.toolProgress === "streaming";
          const previousPending = previous?.kind === "activity_group" && previous.items.some(child =>
            child.kind === "entry" && child.entry.toolProgress === "streaming");
          slot = { group: !pending && !previousPending && previous?.kind === "activity_group" &&
            previous.category === category && previous.scope === itemScope ? previous.id : `activity:${key}` };
        } else slot = {};
        this.slots.set(key, slot);
      }
      if (slot.group) {
        let group = groups.get(slot.group);
        if (!group) {
          group = { kind: "activity_group", id: slot.group, category: category || "tools", scope: scope(item), items: [] };
          groups.set(group.id, group); result.push(group);
        }
        group.items.push(item);
      } else result.push(item);
    }
    return result;
  }
}

/** Derive reading layout without changing transcript identities or execution state. */
export function activityLayout(items: TranscriptItem[], appearance?: ChatAppearance, insideGroup = false): PresentationItem[] {
  const result: PresentationItem[] = [];
  items.forEach((item) => {
    const category = activityCategory(item), rule = category && appearance?.activity[category];
    const hidden = rule?.placement === "hidden" && !importantActivity(item);
    if (!hidden) {
      if (category && rule?.placement === "grouped" && !insideGroup) {
        const previous = result.at(-1), itemScope = scope(item);
        if (previous?.kind === "activity_group" && previous.category === category && previous.scope === itemScope) previous.items.push(item);
        else result.push({ kind: "activity_group", id: `activity:${itemKey(item)}`, category, scope: itemScope, items: [item] });
      } else result.push(item);
    }
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
