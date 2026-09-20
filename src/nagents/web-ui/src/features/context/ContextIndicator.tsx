import type { ContextStats } from "../../types";
import { contextPercent, contextRows, contextSummary, formatTokens } from "./controller";
import "./context.css";

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
  const rows = stats ? contextRows(stats) : [];
  return (
    <div className="context-indicator">
      <button
        type="button"
        className="context-toggle"
        aria-expanded={open}
        aria-controls="context-panel"
        title={stats ? `${stats.total_tokens.toLocaleString()} estimated input tokens` : error}
        onClick={() => setOpen(!open)}
      >
        <span className="context-toggle-label">Context</span>
        {live && <span className="context-live-dot" aria-hidden="true" />}
        <span className="context-total">{stats ? contextSummary(stats) : loading ? "Loading…" : "unavailable"}</span>
      </button>
      {open && (
        <div id="context-panel" className="context-panel" role="region" aria-label="Estimated context breakdown">
          <div className="context-panel-heading"><strong>{live ? "Live context" : "Conversation context"}</strong>{refresh && <button type="button" onClick={refresh} disabled={loading} aria-label="Refresh context estimate">Refresh</button>}</div>
          {stats ? (
            <>
              {stats.context_window ? (
                <div
                  className="context-meter"
                  role="img"
                  aria-label={`${contextPercent(stats)} percent of the context window estimated in use`}
                >
                  <span style={{ width: `${contextPercent(stats)}%` }} />
                </div>
              ) : null}
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
                  ? `${(stats.remaining_tokens ?? 0).toLocaleString()} tokens remaining of ${stats.context_window.toLocaleString()}.`
                  : "Context window unknown for this model."}
              </p>
              {error && <p className="context-note" role="status">{error} Showing the last available estimate.</p>}
              {stats.observed_prompt_tokens != null ? (
                <p className="context-note">
                  Last provider-reported input: {stats.observed_prompt_tokens.toLocaleString()} tokens.
                </p>
              ) : null}
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
