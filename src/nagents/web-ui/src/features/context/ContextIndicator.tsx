import type { ContextStats } from "../../types";
import { contextPercent, contextRows, contextSummary, contextUsage, contextUsageLabel, contextWindowSource, formatTokens } from "./controller.js";

export function ContextIndicator({
  stats,
  error,
  open,
  setOpen,
  live = false,
  loading = false,
  refresh,
}: {
  stats?: ContextStats;
  error: string;
  open: boolean;
  setOpen: (open: boolean) => void;
  live?: boolean;
  loading?: boolean;
  refresh?: () => void;
}) {
  if (!stats && !error && !loading) return null;
  if (stats?.provider === "a2a") return <div className="context-indicator"><span className="context-toggle" title="The remote agent manages its context. This server does not receive its model window or token usage.">Remote context</span></div>;
  const rows = stats ? contextRows(stats) : [];
  return (
    <div className="context-indicator">
      <button
        type="button"
        className="context-toggle"
        aria-expanded={open}
        aria-controls="context-panel"
        aria-label={stats ? `Context ${contextSummary(stats)}` : undefined}
        title={stats ? `${contextUsageLabel(stats)}: ${contextUsage(stats).toLocaleString()} tokens. Window: ${stats.context_window?.toLocaleString() || "unknown"} (${contextWindowSource(stats)}).` : error}
        onClick={() => setOpen(!open)}
      >
        <span className="context-toggle-label">Context</span>
        {live && <span className="context-live-dot" aria-hidden="true" />}
        <span className="context-total">{stats ? contextSummary(stats, true) : loading ? "Loading…" : "unavailable"}</span>
      </button>
      {open && (
        <div id="context-panel" className="context-panel" role="region" aria-label="Context usage and estimated breakdown">
          <div className="context-panel-heading"><strong>{live ? "Live context" : "Conversation context"}</strong>{refresh && <button type="button" onClick={refresh} disabled={loading} aria-label="Refresh context statistics">Refresh</button>}</div>
          {stats ? (
            <>
              {stats.context_window ? (
                <div
                  className="context-meter"
                  role="img"
                  aria-label={`${contextUsageLabel(stats)}: ${contextUsage(stats).toLocaleString()} of ${stats.context_window.toLocaleString()} tokens (${contextWindowSource(stats)})`}
                >
                  <span style={{ width: `${contextPercent(stats)}%` }} />
                </div>
              ) : null}
              <div className="context-usage-row"><span>{contextUsageLabel(stats)}</span><strong>{contextUsage(stats).toLocaleString()}</strong></div>
              {stats.observed_context_tokens != null && <p className="context-note">Input and output from the last response; reported reasoning is counted once. Added messages are estimated.</p>}
              <p className="context-breakdown-label">Estimated next request</p>
              <ul className="context-rows">
                {rows.map((row) => (
                  <li key={row.key}>
                    <span className="context-row-label">{row.label}</span>
                    <span className="context-row-value">{formatTokens(row.tokens)}</span>
                  </li>
                ))}
                {rows.length === 0 ? <li className="context-empty">No context yet</li> : null}
              </ul>
              <div className="context-total-row">
                <span>Total estimated input</span>
                <span>{stats.total_tokens.toLocaleString()}</span>
              </div>
              <p className="context-note">{stats.model}{live ? " · Updates during turns and tool execution" : ""}</p>
              <p className="context-note">
                {stats.context_window
                  ? `${stats.context_window.toLocaleString()} token window · ${contextWindowSource(stats)}. ${Math.abs(stats.context_window - contextUsage(stats)).toLocaleString()} tokens ${contextUsage(stats) > stats.context_window ? "over the limit" : "remaining"}.`
                  : "Context window unknown for this model."}
              </p>
              {error && <p className="context-note" role="status">{error} Showing the last available estimate.</p>}
              <p className="context-note">
                {stats.estimate_method}; cached tokens and attachments are approximate.
              </p>
            </>
          ) : (
            <p className="context-note" role="status">
              {error || "Reading context…"}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
