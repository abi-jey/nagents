import { useEffect, useRef } from "react";
import { restoreDeletionFocus, type DeletionState, type SessionDeletion } from "../../api/deletion.js";

export function DeleteSessionDialog({ state, controller, currentSessionId = state.currentSessionId ?? state.target?.id }: {
  state: DeletionState; controller: SessionDeletion; currentSessionId?: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current;
    element?.showModal(); cancel.current?.focus();
    return () => {
      element?.close();
      restoreDeletionFocus(
        previous instanceof HTMLElement ? previous : null,
        document.querySelector<HTMLElement>("dialog[open] .trash-close") || document.getElementById("composer"),
        document.querySelector<HTMLElement>("#session-navigation.open[role='dialog'][aria-modal='true']"),
      );
    };
  }, []);
  return <dialog ref={dialog} className="approval-dialog delete-session-dialog" aria-labelledby="delete-session-title"
    aria-describedby="delete-session-description" aria-busy={state.pending}
    onCancel={(event) => { event.preventDefault(); event.stopPropagation(); controller.cancel(); }}>
    <header className="approval-heading"><h2 id="delete-session-title">Delete forever?</h2></header>
    <div className="approval-body">
      <p className="delete-session-name">{state.target?.title}</p>
      <p id="delete-session-description">Permanently delete this session’s saved conversation.
        {!state.trash && state.target?.id === currentSessionId
          ? " Your current draft will be discarded only after deletion is confirmed."
          : " Your current conversation and draft are kept."}
        {" This cannot be undone. Workspace files, other sessions, and credentials are kept."}</p>
      {state.error && <p role="alert" className="error-text">{state.error}</p>}
      <p role="status">{state.pending ? "Deleting session…" : ""}</p>
    </div>
    <div className="dialog-actions">
      <button ref={cancel} disabled={state.pending} onClick={() => controller.cancel()}>Cancel</button>
      <button className="cancel danger-action" disabled={state.pending} onClick={() => void controller.confirm()}>Delete forever</button>
    </div>
  </dialog>;
}
