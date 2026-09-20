import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import type { Session } from "../../types";
import type { ReactNode } from "react";
import { SessionMenu } from "./SessionMenu.js";

function WorkspaceDialog({ workspace, name, demo, count, close, settings, settingsDisabled }: {
  workspace: string; name: string; demo: boolean; count: number; close: () => void; settings: () => void; settingsDisabled: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    element?.showModal();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, []);
  return <dialog ref={dialog} className="workspace-dialog" aria-labelledby="workspace-dialog-title"
    onCancel={(event) => { event.preventDefault(); close(); }}>
    <header><h2 id="workspace-dialog-title"><Icon name="folder" />{name}</h2>
      <button type="button" autoFocus onClick={close} aria-label="Close workspace information"><Icon name="close" /></button>
    </header>
    <dl>
      <dt>Workspace folder</dt><dd>{workspace || "Connecting…"}</dd>
      <dt>Mode</dt><dd>{demo ? "Offline demo" : "Live provider"}</dd>
      <dt>Saved sessions</dt><dd>{count}</dd>
    </dl>
    <footer><button type="button" disabled={settingsDisabled} onClick={() => { close(); settings(); }}><Icon name="settings" size={14} /> Workspace settings</button></footer>
  </dialog>;
}

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
  globalSettings = settings,
  settingsDisabled,
  tools = () => {},
  toolsDisabled = settingsDisabled,
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
  globalSettings?: () => void;
  tools?: () => void;
  toolsDisabled?: boolean;
  settingsDisabled: boolean;
  channels: () => void;
  channelsDisabled: boolean;
  demo: boolean;
  close: () => void;
}) {
  const panel = useRef<HTMLElement>(null);
  const [mobile, setMobile] = useState(false);
  const [workspaceOpen, setWorkspaceOpen] = useState(false);
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
        aria-label="New session" title="New session"
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
            aria-label={session.title || "New session"}
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
          <button className="sidebar-utility trash-trigger" title="Trash" aria-label="Trash" disabled={trashDisabled} onClick={trash} aria-haspopup="dialog">
            <Icon name="trash" /><span>Trash</span><Icon name="chevron" size={13} />
          </button>
          <button className="sidebar-utility settings-trigger" title="Global settings" aria-label="Global settings" disabled={settingsDisabled} onClick={globalSettings} aria-haspopup="dialog">
            <Icon name="settings" /><span>Global settings</span><Icon name="chevron" size={13} />
          </button>
          <button className="sidebar-utility channels-trigger" title="Channels" aria-label="Channels" disabled={channelsDisabled} onClick={channels} aria-haspopup="dialog">
            <Icon name="channels" /><span>Channels</span><Icon name="chevron" size={13} />
          </button>
          <button className="sidebar-utility tools-trigger" title="Tools" aria-label="Tools" disabled={toolsDisabled} onClick={tools} aria-haspopup="dialog">
            <Icon name="tools" /><span>Tools</span><Icon name="chevron" size={13} />
          </button>
        </nav>
        <div className="workspace-info">
          <button type="button" className="workspace-trigger" title={workspace || "Workspace information"}
            aria-label={`Workspace information: ${workspaceName}`} aria-haspopup="dialog" onClick={() => setWorkspaceOpen(true)}>
            <span className="workspace-icon"><Icon name="folder" /></span>
            <span className="workspace-caption"><strong>{workspaceName}</strong></span>
            <Icon name="chevron" size={13} />
          </button>
        </div>
      </div>
    </aside>
    {workspaceOpen && <WorkspaceDialog workspace={workspace} name={workspaceName} demo={demo} count={sessions.length} close={() => setWorkspaceOpen(false)} settings={settings} settingsDisabled={settingsDisabled} />}
    </>
  );
}
