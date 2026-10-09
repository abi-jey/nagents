import { useEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { sessionTitle } from "../../api/sessionActions.js";
import type { Session } from "../../types.js";

export type SessionTitleAction = { kind: "rename" | "fork"; session: Session };

export function SessionTitleDialog({ action, save, close, error: serverError }: {
  action: SessionTitleAction; save(title: string): Promise<boolean>; close(): void; error: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState(action.kind === "rename" ? action.session.title : "");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const previous = document.activeElement;
    dialog.current?.showModal();
    return () => { dialog.current?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true }); };
  }, []);
  async function submit() {
    if (pending) return;
    try { sessionTitle(title, action.kind === "rename"); } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Choose a valid title."); return;
    }
    setPending(true); setError("");
    try { if (await save(title)) close(); else setError("Could not update this chat. Check the workspace notice and try again."); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not update this chat."); }
    finally { setPending(false); }
  }
  const label = action.kind === "rename" ? "Rename chat" : "Fork chat";
  return <dialog ref={dialog} className="chat-folder-dialog session-title-dialog" aria-labelledby="session-title-heading"
    onCancel={event => { event.preventDefault(); if (!pending) close(); }}>
    <form onSubmit={event => { event.preventDefault(); void submit(); }}>
      <header><h2 id="session-title-heading"><Icon name={action.kind === "fork" ? "branch" : "edit"} />{label}</h2>
        <button type="button" aria-label="Close chat dialog" disabled={pending} onClick={close}><Icon name="close" /></button>
      </header>
      {action.kind === "fork" && <p>Start an independent chat with the saved context of <strong>{action.session.title || "New session"}</strong>.</p>}
      <label>Chat title{action.kind === "fork" ? " (optional)" : ""}<input autoFocus value={title} maxLength={160}
        required={action.kind === "rename"} disabled={pending} onChange={event => setTitle(event.target.value)}
        placeholder={action.kind === "fork" ? `Fork of ${action.session.title || "New session"}` : "Chat title"} /></label>
      {(serverError || error) && <p role="alert" className="error-text">{serverError || error}</p>}
      <footer><button type="button" disabled={pending} onClick={close}>Cancel</button>
        <button type="submit" className="primary" disabled={pending || (action.kind === "rename" && !title.trim())}>
          {pending ? "Saving…" : label}</button></footer>
    </form>
  </dialog>;
}
