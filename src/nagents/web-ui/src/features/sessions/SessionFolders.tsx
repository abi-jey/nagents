import { useState, type ReactNode } from "react";
import { Icon } from "../../components/Icon.js";
import type { Session } from "../../types.js";
import type { ChatFolderActions } from "./useChatFolders.js";
import { ChatFolderDialog, type FolderAction } from "./ChatFolderDialog.js";

export function SessionFolders({ sessions, selected, folders, renderSession }: {
  sessions: Session[]; selected: string; folders?: ChatFolderActions;
  renderSession: (session: Session, move?: () => void) => ReactNode;
}) {
  const [query, setQuery] = useState("");
  const [action, setAction] = useState<FolderAction>();
  const needle = query.trim().toLocaleLowerCase();
  const groups = folders?.value?.groups || [];
  const membership = folders?.value?.memberships || {};
  const available = !!folders?.ready && !folders.pending;
  const matches = (session: Session) => (session.title || "New session").toLocaleLowerCase().includes(needle);
  const loose = sessions.filter((session) => !membership[session.id] && matches(session));
  const grouped = groups.map((group) => ({ group, sessions: sessions.filter((session) =>
    membership[session.id] === group.id && (group.name.toLocaleLowerCase().includes(needle) || matches(session))),
  })).filter(({ group, sessions: list }) => !needle || list.length || group.name.toLocaleLowerCase().includes(needle));
  function edit(next: FolderAction) { folders?.clearError(); setAction(next); }
  function row(session: Session) {
    return renderSession(session, available ? () => edit({ kind: "move", session }) : undefined);
  }
  return <>
    <div className="workspace-label session-label"><span>Sessions</span><span className="session-count">{sessions.length}</span>
      {folders && <button type="button" className="folder-create" aria-label="New folder" title="New folder"
        disabled={!available} onClick={() => edit({ kind: "create" })}><Icon name="folder" size={14} /><Icon name="plus" size={12} /></button>}
    </div>
    {folders && <div className="session-search"><Icon name="search" size={14} />
      <input type="search" aria-label="Search chats and folders" placeholder="Search chats and folders" value={query}
        onChange={(event) => setQuery(event.target.value)} />
    </div>}
    {folders?.error && !action && <p className="folder-error" role="alert">{folders.error}
      <button type="button" disabled={folders.pending} onClick={() => { folders.clearError(); void folders.refresh(); }}>Refresh</button></p>}
    <nav className="session-list" aria-label="Sessions">
      {grouped.map(({ group, sessions: list }) => {
        const expanded = !group.collapsed || !!needle;
        return <section key={group.id} className={`session-folder${list.some(s => s.id === selected) ? " contains-current" : ""}`}>
          <div className="folder-heading">
            <button type="button" className="folder-toggle" aria-expanded={expanded} aria-controls={`folder-${group.id}`}
              disabled={!available || !!needle} title={needle ? "Search shows matching chats in each folder" : group.name}
              onClick={() => { if (folders) { folders.clearError(); void folders.update(group, undefined, !group.collapsed); } }}>
              <Icon name="chevron" size={12} /><Icon name="folder" size={14} /><span>{group.name}</span><small>{list.length}</small>
            </button>
            <button type="button" className="folder-action" title="Rename folder" aria-label={`Rename folder: ${group.name}`}
              disabled={!available} onClick={() => edit({ kind: "rename", folder: group })}><Icon name="edit" size={13} /></button>
            <button type="button" className="folder-action" title="Remove folder" aria-label={`Remove folder: ${group.name}`}
              disabled={!available} onClick={() => edit({ kind: "remove", folder: group })}><Icon name="close" size={13} /></button>
          </div>
          <div id={`folder-${group.id}`} hidden={!expanded}>
            <ul>{list.map(row)}</ul>
            {!list.length && <p className="folder-empty">Move chats here from their actions menu.</p>}
          </div>
        </section>;
      })}
      {!!loose.length && <section aria-label={groups.length ? "Ungrouped chats" : "Chats"}>
        {!!groups.length && <div className="ungrouped-heading">Ungrouped</div>}
        <ul>{loose.map(row)}</ul>
      </section>}
      {!loose.length && !grouped.length && <p className="empty-sessions">{needle ? "No matching chats or folders." : "Your conversations will appear here."}</p>}
    </nav>
    {action && folders && <ChatFolderDialog action={action} folders={folders} close={() => setAction(undefined)} />}
  </>;
}
