import type { Session } from "../../types";

export function SessionSidebar({
  workspace,
  sessions,
  selected,
  disabled,
  newDisabled = disabled,
  open,
  select,
}: {
  workspace: string;
  sessions: Session[];
  selected: string;
  disabled: boolean;
  newDisabled?: boolean;
  open: boolean;
  select: (id?: string) => void;
}) {
  return (
    <aside
      className={`sidebar ${open ? "open" : ""}`}
      id="session-navigation"
      aria-label="Workspace sessions"
    >
      <div className="workspace-label sr-only">Workspace</div>
      <div className="workspace" title={workspace}>
        {workspace || "Connecting..."}
      </div>
      <button
        className="new-session"
        disabled={disabled || newDisabled}
        onClick={() => select()}
      >
        New session
      </button>
      <div className="workspace-label session-label">Sessions</div>
      <nav aria-label="Sessions">
        {sessions.map((session) => (
          <button
            key={session.id}
            className={`session ${selected === session.id ? "selected" : ""}`}
            aria-current={selected === session.id ? "page" : undefined}
            disabled={disabled}
            onClick={() => select(session.id)}
            title={`${session.title} · Updated ${session.updated_at.slice(0, 10)}`}
          >
            <span>{session.title}</span>
            {session.active_run_id && <small>Working</small>}
            <small className="sr-only">Updated {session.updated_at.slice(0, 10)}</small>
          </button>
        ))}
      </nav>
      <details className="sidebar-footer">
        <summary>Workspace info</summary>
        <div className="sidebar-info-content" role="region" aria-label="Workspace information" tabIndex={0}>
          <p>{workspace}</p>
          <p>Shell is not sandboxed.</p>
        </div>
      </details>
    </aside>
  );
}
