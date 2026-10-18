import type { ChatAppearance } from "../settings/appearance.js";
import { hasResponseDetails } from "./responseMetadata.js";
import type { Entry, TranscriptItem } from "./transcript.js";

export function transcriptEntries(items: TranscriptItem[]): Entry[] {
  return items.flatMap(item => item.kind === "entry" ? [item.entry]
    : item.kind === "thread" ? transcriptEntries(item.thread.items) : transcriptEntries(item.items));
}

/** One agent byline per admitted turn, including reasoning that arrives before text. */
export function agentLabels(entries: Entry[], name: string, names: Map<string, string>, appearance?: ChatAppearance, parent = ""): Map<string, string> {
  const labels = new Map<string, string>(), seen = new Set<string>(), turns = new Map<string, string>();
  let rootName = entries.find(entry => entry.kind === "agent_change" && entry.previousAgentName)?.previousAgentName || name;
  for (const entry of entries) {
    const actor = entry.taskId || "root";
    if (entry.kind === "agent_change" && entry.agentName) rootName = entry.agentName;
    if (entry.kind === "user" && !entry.queued || entry.liveCaption?.speaker === "user") turns.set(actor, entry.id);
    const live = entry.liveCaption?.speaker === "assistant";
    if (entry.kind !== "assistant" && !live) continue;
    const interrupted = entry.responseMetadata && ["failed", "cancelled"].includes(entry.responseMetadata.status);
    const reasoning = appearance?.activity.reasoning.placement !== "hidden" && hasResponseDetails(entry.responseMetadata);
    if (!entry.text && !reasoning && !interrupted) continue;
    const agent = entry.taskId ? names.get(entry.taskId) || entry.taskName || entry.taskId : entry.agentName || rootName;
    const origin = entry.taskId ? entry.parentTaskId ? names.get(entry.parentTaskId) || rootName : rootName : parent;
    const turn = entry.runId || (entry.historyTurn !== undefined ? `history:${entry.historyTurn}` : turns.get(actor) || "opening");
    const key = JSON.stringify([actor, agent, turn, entry.activation || 0, entry.followup || 0, live ? entry.liveCaption?.sessionId : ""]);
    if (seen.has(key)) continue;
    seen.add(key);
    labels.set(entry.id, origin ? `${origin} → ${agent}` : agent);
  }
  return labels;
}
