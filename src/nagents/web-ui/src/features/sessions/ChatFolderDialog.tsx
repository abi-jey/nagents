import { useEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import type { ChatFolder } from "../../api/sessionGroups.js";
import type { Session } from "../../types.js";
import type { ChatFolderActions } from "./useChatFolders.js";
import { folderOptions } from "./sessionTree.js";

export type FolderAction = { kind: "create"; parentId?: string } | { kind: "rename"; folder: ChatFolder } | { kind: "remove"; folder: ChatFolder }
  | { kind: "reparent"; folder: ChatFolder }
  | { kind: "move"; session: Session };

export function ChatFolderDialog({ action, folders, close }: {
  action: FolderAction; folders: ChatFolderActions; close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState("folder" in action ? action.folder.name : "");
  const [destination, setDestination] = useState(action.kind === "move" ? folders.value?.memberships[action.session.id] || ""
    : action.kind === "create" ? action.parentId || "" : action.kind === "reparent" ? action.folder.parent_id : "");
  const groups = folders.value?.groups || [];
  const options = folderOptions(groups, action.kind === "reparent" ? action.folder.id : "");
  function height(id: string): number { return 1 + Math.max(0, ...groups.filter(group => group.parent_id === id).map(group => height(group.id))); }
  const subtreeHeight = action.kind === "reparent" ? height(action.folder.id) : 1;
  const title = action.kind === "create" ? "New folder" : action.kind === "rename" ? "Rename folder"
    : action.kind === "move" ? "Move chat" : action.kind === "reparent" ? "Move folder" : "Remove folder";
  useEffect(() => {
    const previous = document.activeElement;
    dialog.current?.showModal();
    return () => {
      dialog.current?.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);
  async function submit() {
    const result = action.kind === "create" ? await folders.create(name, destination)
      : action.kind === "rename" ? await folders.update(action.folder, name)
      : action.kind === "remove" ? await folders.remove(action.folder)
      : action.kind === "reparent" ? await folders.reparent(action.folder, destination)
      : await folders.move(action.session.id, destination);
    if (result) close();
  }
  return <dialog ref={dialog} className="chat-folder-dialog" aria-labelledby="chat-folder-title"
    onCancel={(event) => { event.preventDefault(); if (!folders.pending) close(); }}>
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <header><h2 id="chat-folder-title"><Icon name="folder" />{title}</h2>
        <button type="button" aria-label="Close folder dialog" disabled={folders.pending} onClick={close}><Icon name="close" /></button>
      </header>
      {action.kind === "move" || action.kind === "reparent" ? <>
        <p className="folder-chat-title">{action.kind === "move" ? action.session.title || "New session" : action.folder.name}</p>
        <label>{action.kind === "move" ? "Folder" : "Parent folder"}<select autoFocus value={destination} disabled={folders.pending}
          onChange={(event) => setDestination(event.target.value)}>
          <option value="">{action.kind === "move" ? "Ungrouped" : "Top level"}</option>
          {options.map(folder => <option key={folder.id} value={folder.id}
            disabled={action.kind === "reparent" && folder.depth + subtreeHeight > 12}>{folder.label}</option>)}
        </select></label>
      </> : action.kind === "remove" ? <p>Remove <strong>{action.folder.name}</strong>? Its chats and direct subfolders move to {groups.find(group => group.id === action.folder.parent_id)?.name || "the top level"}. No chats are deleted.</p>
        : <label>Folder name<input autoFocus value={name} required maxLength={80} disabled={folders.pending}
          onChange={(event) => setName(event.target.value)} placeholder="Project or topic" /></label>}
      {action.kind === "create" && <label>Parent folder<select value={destination} disabled={folders.pending}
        onChange={(event) => setDestination(event.target.value)}><option value="">Top level</option>
        {options.map(folder => <option key={folder.id} value={folder.id} disabled={folder.depth >= 12}>{folder.label}</option>)}
      </select></label>}
      {folders.error && <p role="alert" className="error-text">{folders.error}</p>}
      <footer><button type="button" disabled={folders.pending} onClick={close}>Cancel</button>
        <button type="submit" className={action.kind === "remove" ? "danger-action" : "primary"}
          disabled={folders.pending || !folders.ready || (["create", "rename"].includes(action.kind) && !name.trim())}>
          {folders.pending ? "Saving…" : action.kind === "create" ? "Create folder" : action.kind === "move" ? "Move chat" : action.kind === "reparent" ? "Move folder"
            : action.kind === "remove" ? "Remove folder" : "Save"}
        </button></footer>
    </form>
  </dialog>;
}
