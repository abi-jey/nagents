import type { ContextComponent, ContextStats } from "../../types";

export function contextRows(stats: ContextStats): ContextComponent[] {
  return stats.components.filter((component) => component.tokens > 0);
}

export function formatTokens(tokens: number): string {
  if (tokens >= 1_000_000) return `${Number((tokens / 1_000_000).toFixed(1))}M`;
  if (tokens >= 1000) return `${(tokens / 1000).toFixed(tokens >= 10000 ? 0 : 1)}k`;
  return String(tokens);
}

export function contextUsage(stats: ContextStats): number {
  return stats.observed_prompt_tokens ?? stats.total_tokens;
}

export function contextUsageLabel(stats: ContextStats): string {
  return stats.observed_prompt_tokens != null ? "Last provider-reported input" : "Estimated input";
}

export function contextWindowLabel(stats: ContextStats, concise = false): string {
  if (!stats.context_window) return "Unknown";
  return stats.context_window_source === "heuristic"
    ? `~${formatTokens(stats.context_window)}${concise ? "" : " (guessed)"}` : formatTokens(stats.context_window);
}

export function contextWindowSource(stats: ContextStats): string {
  return { configured: "configured limit", provider: "provider metadata", heuristic: "model-name guess" }[stats.context_window_source];
}

export function contextSummary(stats: ContextStats, concise = false): string {
  const total = `${stats.observed_prompt_tokens == null ? "~" : ""}${formatTokens(contextUsage(stats))}`;
  if (stats.context_window && stats.context_window > 0) {
    return `${total} / ${contextWindowLabel(stats, concise)}`;
  }
  return total;
}

export function contextPercent(stats: ContextStats): number {
  if (!stats.context_window || stats.context_window <= 0) return 0;
  return Math.min(100, Math.round((contextUsage(stats) / stats.context_window) * 100));
}

const contextEvents = new Set([
  "run_started", "user_message", "tool_call", "tool_result", "tool_output",
  "text_chunk", "text_done", "reasoning_done", "done", "run_finished",
  "compaction_started", "compaction_done", "task_started", "task_completed", "task_notification",
  "agent_selection_changed", "agent_changed", "agent_profiles_changed",
]);

export function changesContext(event: string): boolean { return contextEvents.has(event); }

/** Coalesce event bursts without overlapping reads or losing changes during a read. */
export class ContextRefresh {
  private disposed = false;
  private dirty = false;
  private reading = false;
  private cancelTimer?: () => void;
  private controller?: AbortController;
  constructor(private readonly options: {
    read: (signal: AbortSignal) => Promise<ContextStats>;
    accept: (stats: ContextStats) => void;
    error: (message: string) => void;
    loading: (loading: boolean) => void;
    schedule?: (action: () => void, delay: number) => () => void;
  }) {}

  refresh(immediate = false) {
    if (this.disposed) return;
    this.dirty = true;
    if (this.reading || this.cancelTimer) return;
    const schedule = this.options.schedule || ((action, delay) => { const timer = setTimeout(action, delay); return () => clearTimeout(timer); });
    this.cancelTimer = schedule(() => { this.cancelTimer = undefined; void this.fetch(); }, immediate ? 0 : 150);
  }

  private async fetch() {
    if (this.disposed) return;
    this.reading = true; this.dirty = false;
    const controller = this.controller = new AbortController();
    this.options.loading(true);
    try {
      const stats = await this.options.read(controller.signal);
      if (!this.disposed) { this.options.accept(stats); this.options.error(""); }
    } catch (cause) {
      if (!this.disposed && !controller.signal.aborted) this.options.error(cause instanceof Error ? cause.message : "Context statistics unavailable.");
    } finally {
      this.reading = false;
      if (!this.disposed) {
        this.options.loading(false);
        if (this.dirty) this.refresh();
      }
    }
  }

  dispose() {
    this.disposed = true; this.cancelTimer?.(); this.controller?.abort();
  }
}
