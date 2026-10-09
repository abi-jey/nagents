import { useEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import type { ChatFolder } from "../../api/sessionGroups.js";
import type { Session } from "../../types.js";
import type { ChatFolderActions } from "./useChatFolders.js";

export type FolderAction = { kind: "create" } | { kind: "rename"; folder: ChatFolder } | { kind: "remove"; folder: ChatFolder }
  | { kind: "move"; session: Session };

export function ChatFolderDialog({ action, folders, close }: {
  action: FolderAction; folders: ChatFolderActions; close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [name, setName] = useState("folder" in action ? action.folder.name : "");
  const [destination, setDestination] = useState(action.kind === "move" ? folders.value?.memberships[action.session.id] || "" : "");
  const title = action.kind === "create" ? "New folder" : action.kind === "rename" ? "Rename folder"
    : action.kind === "move" ? "Move chat" : "Remove folder";
  useEffect(() => {
    const previous = document.activeElement;
    dialog.current?.showModal();
    return () => {
      dialog.current?.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);
  async function submit() {
    const result = action.kind === "create" ? await folders.create(name)
      : action.kind === "rename" ? await folders.update(action.folder, name)
      : action.kind === "remove" ? await folders.remove(action.folder)
      : await folders.move(action.session.id, destination);
    if (result) close();
  }
  return <dialog ref={dialog} className="chat-folder-dialog" aria-labelledby="chat-folder-title"
    onCancel={(event) => { event.preventDefault(); if (!folders.pending) close(); }}>
    <form onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      <header><h2 id="chat-folder-title"><Icon name="folder" />{title}</h2>
        <button type="button" aria-label="Close folder dialog" disabled={folders.pending} onClick={close}><Icon name="close" /></button>
      </header>
      {action.kind === "move" ? <>
        <p className="folder-chat-title">{action.session.title || "New session"}</p>
        <label>Folder<select autoFocus value={destination} disabled={folders.pending}
          onChange={(event) => setDestination(event.target.value)}>
          <option value="">Ungrouped</option>
          {folders.value?.groups.map((folder) => <option key={folder.id} value={folder.id}>{folder.name}</option>)}
        </select></label>
      </> : action.kind === "remove" ? <p>Remove <strong>{action.folder.name}</strong>? Its chats will stay available under Ungrouped.</p>
        : <label>Folder name<input autoFocus value={name} required maxLength={80} disabled={folders.pending}
          onChange={(event) => setName(event.target.value)} placeholder="Project or topic" /></label>}
      {folders.error && <p role="alert" className="error-text">{folders.error}</p>}
      <footer><button type="button" disabled={folders.pending} onClick={close}>Cancel</button>
        <button type="submit" className={action.kind === "remove" ? "danger-action" : "primary"}
          disabled={folders.pending || !folders.ready || (["create", "rename"].includes(action.kind) && !name.trim())}>
          {folders.pending ? "Saving…" : action.kind === "create" ? "Create folder" : action.kind === "move" ? "Move chat"
            : action.kind === "remove" ? "Remove folder" : "Save"}
        </button></footer>
    </form>
  </dialog>;
}
