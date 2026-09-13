import { useEffect, useRef } from "react";
import type { DeletionState, SessionDeletion } from "../../api/deletion";

export function DeleteSessionDialog({ state, controller }: { state: DeletionState; controller: SessionDeletion }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current;
    element?.showModal(); cancel.current?.focus();
    return () => { element?.close(); if (previous instanceof HTMLElement) previous.focus(); };
  }, []);
  return <dialog ref={dialog} className="approval-dialog delete-session-dialog" aria-labelledby="delete-session-title"
    aria-describedby="delete-session-description" aria-busy={state.pending}
    onCancel={(event) => { event.preventDefault(); controller.cancel(); }}>
    <header className="approval-heading"><h2 id="delete-session-title">Delete session?</h2></header>
    <div className="approval-body">
      <p className="delete-session-name">{state.target?.title}</p>
      <p id="delete-session-description">Permanently delete this session’s saved conversation and discard your current draft. This cannot be undone. Workspace files, other sessions, and credentials are kept.</p>
      {state.error && <p role="alert" className="error-text">{state.error}</p>}
      <p role="status">{state.pending ? "Deleting session…" : ""}</p>
    </div>
    <div className="dialog-actions">
      <button ref={cancel} disabled={state.pending} onClick={() => controller.cancel()}>Cancel</button>
      <button className="cancel" disabled={state.pending} onClick={() => void controller.confirm()}>Delete session</button>
    </div>
  </dialog>;
}
