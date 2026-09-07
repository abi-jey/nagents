import type { Session } from "../../types";

export function SessionSidebar({
  workspace,
  sessions,
  selected,
  disabled,
  open,
  select,
}: {
  workspace: string;
  sessions: Session[];
  selected: string;
  disabled: boolean;
  open: boolean;
  select: (id?: string) => void;
}) {
  return (
    <aside
      className={`sidebar ${open ? "open" : ""}`}
      id="session-navigation"
      aria-label="Workspace sessions"
    >
      <div className="workspace-label">Workspace</div>
      <div className="workspace" title={workspace}>
        {workspace || "Connecting..."}
      </div>
      <button
        className="new-session"
        disabled={disabled}
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
            title={session.title}
          >
            <span>{session.title}</span>
            <small>{session.updated_at.slice(0, 10)}</small>
          </button>
        ))}
      </nav>
      <div className="sidebar-footer">
        Loopback only
        <p>Shell is not sandboxed.</p>
      </div>
    </aside>
  );
}
