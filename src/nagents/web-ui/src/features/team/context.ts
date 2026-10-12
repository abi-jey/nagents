import { formatTokens } from "../context/controller.js";
import type { TeamAgent } from "./model.js";

export function compactLabel(agent: TeamAgent): string {
  if (agent.compactTrigger === "off") return "Compaction off";
  if (agent.compactTrigger === "messages") return agent.compactMessages ? `Compact at ${agent.compactMessages} messages` : "Message compaction";
  const budget = agent.compactTokens;
  const threshold = agent.compactThreshold;
  if (threshold) return `Compact at ${formatTokens(threshold)}${budget ? ` (${Math.round(100 * threshold / budget)}% of ${formatTokens(budget)})` : ""}`;
  if (agent.compactTrigger === "auto") return "Auto compaction";
  return "";
}

export function compactionWarning(agent: TeamAgent): string {
  const context = agent.context;
  if (!context?.context_window || context.context_window_source === "heuristic" || !["tokens", "auto"].includes(agent.compactTrigger || "")) return "";
  if (agent.compactTokens && agent.compactTokens > context.context_window)
    return `${formatTokens(agent.compactTokens)} budget exceeds the ${formatTokens(context.context_window)} model limit.`;
  if (agent.compactThreshold && agent.compactThreshold > context.context_window)
    return `Compaction starts beyond the ${formatTokens(context.context_window)} model limit.`;
  return "";
}
