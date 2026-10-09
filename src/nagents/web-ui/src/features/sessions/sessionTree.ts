import type { ChatFolder } from "../../api/sessionGroups.js";
import type { Session } from "../../types.js";

export type ChatBranch = { session: Session; children: ChatBranch[]; origin: string };
export type FolderBranch = { folder: ChatFolder; folders: FolderBranch[]; chats: ChatBranch[]; count: number; containsCurrent: boolean };

export function selectedPath(sessions: Session[], folders: ChatFolder[], membership: Record<string, string>, selected: string) {
  const byId = new Map(sessions.map(session => [session.id, session]));
  const forkIds: string[] = [], folderIds: string[] = [];
  const folder = membership[selected] || "";
  const seen = new Set([selected]);
  let parent = byId.get(selected)?.forked_from || "";
  while (parent && byId.has(parent) && !seen.has(parent) && seen.size < 64 && (membership[parent] || "") === folder) {
    forkIds.push(parent); seen.add(parent); parent = byId.get(parent)?.forked_from || "";
  }
  parent = folder;
  while (parent && folderIds.length < 12 && !folderIds.includes(parent)) {
    folderIds.push(parent); parent = folders.find(item => item.id === parent)?.parent_id || "";
  }
  return { forkIds, folderIds };
}

/** Build a forest without confusing copied conversations with agent child sessions. */
export function chatBranches(sessions: Session[], membership: Record<string, string>, folderId: string): ChatBranch[] {
  const all = new Map(sessions.map(session => [session.id, session]));
  const local = sessions.filter(session => (membership[session.id] || "") === folderId);
  const nodes = new Map(local.map(session => [session.id, { session, children: [], origin: "" } as ChatBranch]));
  const roots: ChatBranch[] = [];
  for (const session of local) {
    const node = nodes.get(session.id)!;
    const parent = nodes.get(session.forked_from || "");
    // Malformed legacy ancestry must never hide a conversation or recurse forever.
    const seen = new Set([session.id]);
    let ancestor = parent;
    // Bound both traversal and render depth for unusually long legacy lineages.
    while (ancestor && !seen.has(ancestor.session.id) && seen.size < 64) {
      seen.add(ancestor.session.id); ancestor = nodes.get(ancestor.session.forked_from || "");
    }
    if (parent && !ancestor) parent.children.push(node);
    else {
      if (session.forked_from) node.origin = all.get(session.forked_from)?.title || "unavailable chat";
      roots.push(node);
    }
  }
  return roots;
}

function filterChats(branches: ChatBranch[], query: string, includeAll: boolean): ChatBranch[] {
  return branches.flatMap(branch => {
    const matches = includeAll || branch.session.title.toLocaleLowerCase().includes(query);
    const children = filterChats(branch.children, query, matches);
    return matches || children.length ? [{ ...branch, children }] : [];
  });
}
function contains(branches: ChatBranch[], selected: string): boolean {
  return branches.some(branch => branch.session.id === selected || contains(branch.children, selected));
}
function count(branches: ChatBranch[]): number {
  return branches.reduce((total, branch) => total + 1 + count(branch.children), 0);
}

export function sessionTree(sessions: Session[], folders: ChatFolder[], membership: Record<string, string>, query = "", selected = "") {
  const needle = query.trim().toLocaleLowerCase();
  function descend(parentId: string, includeAll: boolean): FolderBranch[] {
    return folders.filter(folder => folder.parent_id === parentId).flatMap(folder => {
      const match = includeAll || folder.name.toLocaleLowerCase().includes(needle);
      const children = descend(folder.id, match);
      const chats = filterChats(chatBranches(sessions, membership, folder.id), needle, match);
      return !needle || match || children.length || chats.length ? [{ folder, folders: children, chats,
        count: count(chats) + children.reduce((total, child) => total + child.count, 0),
        containsCurrent: contains(chats, selected) || children.some(child => child.containsCurrent),
      }] : [];
    });
  }
  return { folders: descend("", false), chats: filterChats(chatBranches(sessions, membership, ""), needle, false) };
}

/** Select labels include ancestry, so identically named folders remain distinguishable. */
export function folderOptions(folders: ChatFolder[], movingId = ""): { id: string; label: string; depth: number }[] {
  function descend(parentId: string, prefix: string, depth: number): { id: string; label: string; depth: number }[] {
    return folders.filter(folder => folder.parent_id === parentId && folder.id !== movingId).flatMap(folder => {
      const label = prefix ? `${prefix} / ${folder.name}` : folder.name;
      return [{ id: folder.id, label, depth }, ...descend(folder.id, label, depth + 1)];
    });
  }
  return descend("", "", 1);
}
