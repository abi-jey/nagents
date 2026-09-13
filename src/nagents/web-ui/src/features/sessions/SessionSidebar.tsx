import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import type { Session } from "../../types";
import type { ReactNode } from "react";
import { SessionMenu } from "./SessionMenu.js";

export function SessionSidebar({
  workspace,
  sessions,
  selected,
  disabled,
  newDisabled = disabled,
  open,
  select,
  remove,
  canDelete,
  permanent,
  trash,
  trashDisabled,
  notice,
  settings,
  settingsDisabled,
  channels,
  channelsDisabled,
  demo,
  close,
}: {
  workspace: string;
  sessions: Session[];
  selected: string;
  disabled: boolean;
  newDisabled?: boolean;
  open: boolean;
  select: (id?: string) => void;
  remove: (session: Session) => void;
  canDelete: (id: string) => boolean;
  permanent: (session: Session) => void;
  trash: () => void;
  trashDisabled: boolean;
  notice?: ReactNode;
  settings: () => void;
  settingsDisabled: boolean;
  channels: () => void;
  channelsDisabled: boolean;
  demo: boolean;
  close: () => void;
}) {
  const panel = useRef<HTMLElement>(null);
  const [mobile, setMobile] = useState(false);
  const workspaceName = workspace.split(/[\\/]/).filter(Boolean).at(-1) || "Workspace";
  useEffect(() => {
    const query = window.matchMedia("(max-width: 760px)");
    const update = () => setMobile(query.matches);
    update(); query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);
  useLayoutEffect(() => {
    if (!open || !mobile || !panel.current) return;
    const element = panel.current;
    const previous = document.activeElement;
    const background = [...document.querySelectorAll<HTMLElement>(".topbar, .conversation, .composer-area")];
    const wasInert = background.map((item) => item.inert);
    background.forEach((item) => { item.inert = true; });
    element.querySelector<HTMLButtonElement>(".sidebar-close")?.focus();
    function keydown(event: KeyboardEvent) {
      if (document.querySelector("dialog[open], [popover]:popover-open")) return;
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); close(); }
      if (event.key !== "Tab") return;
      const targets = [...element.querySelectorAll<HTMLElement>("button:not(:disabled), summary, [tabindex='0']")]
        .filter((item) => {
          if (!item.getClientRects().length || item.checkVisibility?.() === false) return false;
          // Closed details can retain layout rectangles while their contents
          // cannot receive focus. Keep only their visible summary in the loop.
          for (let parent = item.parentElement; parent && parent !== element; parent = parent.parentElement)
            if (parent instanceof HTMLDetailsElement && !parent.open &&
                !parent.querySelector(":scope > summary")?.contains(item)) return false;
          return true;
        });
      const first = targets[0], last = targets.at(-1);
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      background.forEach((item, index) => { item.inert = wasInert[index]; });
      const trigger = document.querySelector<HTMLButtonElement>(".nav-toggle");
      if (trigger?.getClientRects().length && !trigger.disabled) trigger.focus();
      else if (previous instanceof HTMLElement && previous.isConnected && previous.getClientRects().length) previous.focus();
    };
  }, [open, mobile, close, selected]);
  return (
    <>
    {open && <button className="sidebar-scrim" aria-hidden="true" tabIndex={-1} onClick={close} />}
    <aside
      ref={panel}
      className={`sidebar ${open ? "open" : ""}`}
      id="session-navigation"
      aria-label="Workspace sessions"
      role={mobile && open ? "dialog" : undefined}
      aria-modal={mobile && open ? true : undefined}
    >
      <div className="sidebar-header">
        <div className="nav-brand"><span className="brand-mark" aria-hidden="true">n</span><span>ngn<span className="brand-caption">Workspace</span></span></div>
        <button className="sidebar-close" aria-label="Close sessions" title="Close sessions" onClick={close}><Icon name="close" /></button>
      </div>
      <div className="sidebar-create">
      <button
        className="new-session"
        disabled={disabled || newDisabled}
        onClick={() => select()}
      >
        <Icon name="plus" /> <span>New session</span>
      </button>
      </div>
      <div className="workspace-label session-label"><span>Sessions</span><span className="session-count">{sessions.length}</span></div>
      <nav className="session-list" aria-label="Sessions">
        <ul>
        {sessions.map((session) => (
          <li key={session.id} className={`session-row${selected === session.id ? " selected" : ""}`} data-session-id={session.id}>
          <button
            className={`session ${selected === session.id ? "selected" : ""}`}
            aria-current={selected === session.id ? "page" : undefined}
            disabled={disabled}
            onClick={() => select(session.id)}
            title={`${session.title} · Updated ${session.updated_at.slice(0, 10)}`}
          >
            <Icon name="chat" size={15} />
            <span className="session-title">{session.title || "New session"}</span>
            {session.active_run_id && <span className="session-working" title="Working"><span className="sr-only">Working</span></span>}
            <small className="sr-only">Updated {session.updated_at.slice(0, 10)}</small>
          </button>
          <button className="session-delete" aria-label={`Move to Trash: ${session.title || "New session"}`}
            title="Move to Trash" disabled={!canDelete(session.id) || !!session.active_run_id}
            onClick={() => remove(session)}><Icon name="trash" size={15} /></button>
          <SessionMenu session={session} disabled={!canDelete(session.id) || !!session.active_run_id} permanent={permanent} />
          </li>
        ))}
        </ul>
        {!sessions.length && <p className="empty-sessions">Your conversations will appear here.</p>}
      </nav>
      <div className="sidebar-footer">
        {notice}
        <nav className="sidebar-utilities" aria-label="Workspace controls">
          <button className="sidebar-utility trash-trigger" disabled={trashDisabled} onClick={trash} aria-haspopup="dialog">
            <Icon name="trash" /><span>Trash</span><Icon name="chevron" size={13} />
          </button>
          <button className="sidebar-utility settings-trigger" disabled={settingsDisabled} onClick={settings} aria-haspopup="dialog">
            <Icon name="settings" /><span>Settings</span><Icon name="chevron" size={13} />
          </button>
          <button className="sidebar-utility channels-trigger" disabled={channelsDisabled} onClick={channels} aria-haspopup="dialog">
            <Icon name="channels" /><span>Channels</span><Icon name="chevron" size={13} />
          </button>
        </nav>
        <details className="workspace-info">
          <summary title={workspace || "Workspace information"}>
            <span className="workspace-icon"><Icon name="folder" /></span>
            <span className="workspace-caption"><strong>{workspaceName}</strong><span className={`mode-badge${demo ? " demo" : ""}`}>{demo ? "Offline demo" : "Live provider"}</span></span>
            <Icon name="chevron" size={13} />
          </summary>
          <div className="sidebar-info-content" role="region" aria-label="Workspace information" tabIndex={0}>
            <p>{workspace || "Connecting…"}</p>
            <p>Shared workspace and saved sessions. Shell is not sandboxed.</p>
          </div>
        </details>
      </div>
    </aside>
    </>
  );
}
