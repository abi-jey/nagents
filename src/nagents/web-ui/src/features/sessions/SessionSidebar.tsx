import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import type { Session } from "../../types";
import type { ReactNode } from "react";
import { SessionRow } from "./SessionRow.js";
import { SessionFolders } from "./SessionFolders.js";
import type { ChatFolderActions } from "./useChatFolders.js";
import { WorkspaceInfoDialog, type WorkspaceConnection } from "./WorkspaceInfoDialog.js";

export function SessionSidebar({
  workspace,
  connection,
  sessions,
  folders,
  selected,
  disabled,
  newDisabled = disabled,
  open,
  select,
  remove,
  canDelete,
  permanent,
  rename,
  fork,
  canFork = () => false,
  trash,
  trashDisabled,
  notice,
  settings,
  settingsDisabled,
  tools = () => {},
  toolsDisabled = settingsDisabled,
  designer,
  designerDisabled = settingsDisabled,
  channels,
  channelsDisabled,
  demo,
  close,
}: {
  workspace: string;
  connection?: WorkspaceConnection;
  sessions: Session[];
  folders?: ChatFolderActions;
  selected: string;
  disabled: boolean;
  newDisabled?: boolean;
  open: boolean;
  select: (id?: string) => void;
  remove: (session: Session) => void;
  canDelete: (id: string) => boolean;
  permanent: (session: Session) => void;
  rename?: (session: Session) => void;
  fork?: (session: Session) => void;
  canFork?: (id: string) => boolean;
  trash: () => void;
  trashDisabled: boolean;
  notice?: ReactNode;
  settings: () => void;
  tools?: () => void;
  toolsDisabled?: boolean;
  designer?: () => void;
  designerDisabled?: boolean;
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
      const targets = [...element.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), select:not(:disabled), summary, [tabindex='0']")]
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
      <SessionFolders sessions={sessions} selected={selected} folders={folders} workspace={workspace} renderSession={(session, move) => (
          <SessionRow key={session.id} session={session} selected={selected === session.id} disabled={disabled}
            canDelete={canDelete(session.id)} canFork={canFork(session.id)} select={select} remove={remove}
            permanent={permanent} rename={rename} fork={fork} move={move} />
        )} />
      <div className="sidebar-footer">
        {notice}
        <nav className="sidebar-utilities" aria-label="Workspace controls">
          <button className="sidebar-utility trash-trigger" title="Trash" aria-label="Trash" disabled={trashDisabled} onClick={trash} aria-haspopup="dialog">
            <Icon name="trash" /><span>Trash</span><Icon name="chevron" size={13} />
          </button>
          <button className="sidebar-utility settings-trigger" title="Settings" aria-label="Settings" disabled={settingsDisabled} onClick={settings} aria-haspopup="dialog">
            <Icon name="settings" /><span>Settings</span><Icon name="chevron" size={13} />
          </button>
          <button className="sidebar-utility channels-trigger" title="Channels" aria-label="Channels" disabled={channelsDisabled} onClick={channels} aria-haspopup="dialog">
            <Icon name="channels" /><span>Channels</span><Icon name="chevron" size={13} />
          </button>
          <button className="sidebar-utility tools-trigger" title="Tools" aria-label="Tools" disabled={toolsDisabled} onClick={tools} aria-haspopup="dialog">
            <Icon name="tools" /><span>Tools</span><Icon name="chevron" size={13} />
          </button>
          {designer && <button className="sidebar-utility designer-trigger" title="Experimental agent designer" aria-label="Agent Designer" disabled={designerDisabled} onClick={designer}>
            <Icon name="channels" /><span>Agent Designer</span><Icon name="chevron" size={13} />
          </button>}
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
    {workspaceOpen && <WorkspaceInfoDialog workspace={workspace} name={workspaceName} demo={demo} count={sessions.length}
      running={sessions.filter(session => session.active_run_id).length} connection={connection} close={() => setWorkspaceOpen(false)}
      settings={settings} settingsDisabled={settingsDisabled} tools={tools} toolsDisabled={toolsDisabled} />}
    </>
  );
}
