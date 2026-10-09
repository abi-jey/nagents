import { useEffect, useState, type ReactNode } from "react";
import { Icon } from "../../components/Icon.js";
import type { Session } from "../../types.js";
import type { ChatFolderActions } from "./useChatFolders.js";
import { ChatFolderDialog, type FolderAction } from "./ChatFolderDialog.js";
import { sessionTree, type ChatBranch, type FolderBranch } from "./sessionTree.js";
import { TreeActions } from "./TreeActions.js";

export function SessionFolders({ sessions, selected, folders, renderSession, workspace = "" }: {
  sessions: Session[]; selected: string; folders?: ChatFolderActions; workspace?: string;
  renderSession: (session: Session, move?: () => void) => ReactNode;
}) {
  const [query, setQuery] = useState("");
  const [action, setAction] = useState<FolderAction>();
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  const storageKey = `ngn.fork-branches:${workspace}`;
  useEffect(() => {
    try {
      const saved: unknown = JSON.parse(localStorage.getItem(storageKey) || "[]");
      setCollapsed(new Set(Array.isArray(saved) ? saved.filter((id): id is string => typeof id === "string").slice(0,2048) : []));
    } catch { setCollapsed(new Set()); }
  }, [storageKey]);
  function toggleBranch(id: string) {
    setCollapsed(previous => {
      const next = new Set(previous);
      if (next.has(id)) next.delete(id); else next.add(id);
      try { localStorage.setItem(storageKey, JSON.stringify([...next].slice(-2048))); } catch { /* Browser storage is optional. */ }
      return next;
    });
  }
  const needle = query.trim().toLocaleLowerCase();
  const groups = folders?.value?.groups || [];
  const membership = folders?.value?.memberships || {};
  const available = !!folders?.ready && !folders.pending;
  const tree = sessionTree(sessions, groups, membership, needle, selected);
  function edit(next: FolderAction) { folders?.clearError(); setAction(next); }
  function chats(branches: ChatBranch[], depth = 0): ReactNode {
    return <ul>{branches.map(branch => {
      const { session, children, origin } = branch;
      const expanded = !!needle || !collapsed.has(session.id);
      return <li key={session.id} className="session-branch">
        <div className="session-branch-heading">
          {!!children.length && <button type="button" className="branch-toggle" aria-label={`${expanded ? "Collapse" : "Expand"} forks of ${session.title || "New session"}`}
            aria-expanded={expanded} aria-controls={`forks-${session.id}`} disabled={!!needle} onClick={() => toggleBranch(session.id)}>
            <Icon name="chevron" size={11} />
          </button>}
          {renderSession(session, available ? () => edit({ kind: "move", session }) : undefined)}
        </div>
        {origin && <small className="fork-origin" title={`Fork of ${origin}`}><Icon name="branch" size={11} />Fork of {origin}</small>}
        {!!children.length && <div className={`session-branch-children${depth >= 4 ? " compact-indent" : ""}`} id={`forks-${session.id}`} hidden={!expanded}>{chats(children, depth + 1)}</div>}
      </li>;
    })}</ul>;
  }
  function folder(branch: FolderBranch, depth: number): ReactNode {
    const group = branch.folder;
    const expanded = !group.collapsed || !!needle;
    return <section key={group.id} className={`session-folder${branch.containsCurrent ? " contains-current" : ""}`}>
      <div className="folder-heading">
        <button type="button" className="folder-toggle" aria-expanded={expanded} aria-controls={`folder-${group.id}`}
          disabled={!available || !!needle} title={needle ? "Search shows matching chats in each folder" : group.name}
          onClick={() => { if (folders) { folders.clearError(); void folders.update(group, undefined, !group.collapsed); } }}>
          <Icon name="chevron" size={12} /><Icon name="folder" size={14} /><span>{group.name}</span><small>{branch.count}</small>
        </button>
        <TreeActions label={`folder ${group.name}`} className="folder-action" items={[
          { label: "New subfolder…", icon: "plus", disabled: !available || depth >= 12, run: () => edit({ kind: "create", parentId: group.id }) },
          { label: "Rename folder…", icon: "edit", disabled: !available, run: () => edit({ kind: "rename", folder: group }) },
          { label: "Move folder…", icon: "folder", disabled: !available, run: () => edit({ kind: "reparent", folder: group }) },
          { label: "Remove folder…", icon: "close", disabled: !available, run: () => edit({ kind: "remove", folder: group }) },
        ]} />
      </div>
      <div id={`folder-${group.id}`} className={`folder-children${depth >= 4 ? " compact-indent" : ""}`} hidden={!expanded}>
        {branch.folders.map(child => folder(child, depth + 1))}
        {!!branch.chats.length && chats(branch.chats, depth)}
        {!branch.count && !branch.folders.length && <p className="folder-empty">Move chats here from their actions menu.</p>}
      </div>
    </section>;
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
      {tree.folders.map(branch => folder(branch, 1))}
      {!!tree.chats.length && <section aria-label={groups.length ? "Ungrouped chats" : "Chats"}>
        {!!groups.length && <div className="ungrouped-heading">Ungrouped</div>}
        {chats(tree.chats)}
      </section>}
      {!tree.chats.length && !tree.folders.length && <p className="empty-sessions">{needle ? "No matching chats or folders." : "Your conversations will appear here."}</p>}
    </nav>
    {action && folders && <ChatFolderDialog action={action} folders={folders} close={() => setAction(undefined)} />}
  </>;
}
