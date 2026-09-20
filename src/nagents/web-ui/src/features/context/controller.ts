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

const contextEvents = new Set([
  "run_started", "user_message", "tool_call", "tool_result", "tool_output",
  "text_chunk", "text_done", "reasoning_done", "done", "run_finished",
  "compaction_started", "compaction_done", "task_started", "task_completed", "task_notification",
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
