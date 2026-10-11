import { useEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { sessionTitle } from "../../api/sessionActions.js";
import type { Session } from "../../types.js";

export function SessionTitleDialog({ session, save, close, error: serverError }: {
  session: Session; save(title: string): Promise<boolean>; close(): void; error: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [title, setTitle] = useState(session.title);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => {
    const previous = document.activeElement;
    dialog.current?.showModal();
    return () => { dialog.current?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true }); };
  }, []);
  async function submit() {
    if (pending) return;
    try { sessionTitle(title, true); } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Choose a valid title."); return;
    }
    setPending(true); setError("");
    try { if (await save(title)) close(); else setError("Could not update this chat. Check the workspace notice and try again."); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "Could not update this chat."); }
    finally { setPending(false); }
  }
  return <dialog ref={dialog} className="chat-folder-dialog session-title-dialog" aria-labelledby="session-title-heading"
    onCancel={event => { event.preventDefault(); if (!pending) close(); }}>
    <form onSubmit={event => { event.preventDefault(); void submit(); }}>
      <header><h2 id="session-title-heading"><Icon name="edit" />Rename chat</h2>
        <button type="button" aria-label="Close chat dialog" disabled={pending} onClick={close}><Icon name="close" /></button>
      </header>
      <label>Chat title<input autoFocus value={title} maxLength={160}
        required disabled={pending} onChange={event => setTitle(event.target.value)} placeholder="Chat title" /></label>
      {(serverError || error) && <p role="alert" className="error-text">{serverError || error}</p>}
      <footer><button type="button" disabled={pending} onClick={close}>Cancel</button>
        <button type="submit" className="primary" disabled={pending || !title.trim()}>
          {pending ? "Saving…" : "Rename chat"}</button></footer>
    </form>
  </dialog>;
}
