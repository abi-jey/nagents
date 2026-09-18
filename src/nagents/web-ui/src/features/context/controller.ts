import type { ContextComponent, ContextStats } from "../../types";

export function contextRows(stats: ContextStats): ContextComponent[] {
  return stats.components.filter((component) => component.tokens > 0);
}

export function formatTokens(tokens: number): string {
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(tokens >= 10000 ? 0 : 1)}k`;
  return String(tokens);
}

export function contextSummary(stats: ContextStats): string {
  const total = `~${formatTokens(stats.total_tokens)}`;
  if (stats.context_window && stats.context_window > 0) {
    return `${total} / ${formatTokens(stats.context_window)}`;
  }
  return total;
}

export function contextPercent(stats: ContextStats): number {
  if (!stats.context_window || stats.context_window <= 0) return 0;
  return Math.min(100, Math.round((stats.total_tokens / stats.context_window) * 100));
}
