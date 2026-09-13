import { useEffect, useRef } from "react";
import { restoreDeletionFocus } from "../../api/deletion.js";
import type { TrashItem } from "../../api/trash.js";
import type { TrashController, TrashState } from "./trashController.js";

export const trashDate = (seconds: number) => new Date(seconds * 1000).toLocaleString();
async function restoreAndFocus(controller: TrashController, item: TrashItem) {
  const previous = document.activeElement;
  try {
    await controller.restore(item);
    requestAnimationFrame(() => {
      if (document.activeElement !== previous && document.activeElement !== document.body) return;
      const open = document.querySelector<HTMLElement>("dialog[open] .trash-open-restored:not(:disabled)") ||
        document.querySelector<HTMLElement>(".trash-open-restored:not(:disabled)");
      restoreDeletionFocus(open, document.querySelector<HTMLElement>("dialog[open] .trash-close") || document.getElementById("composer"),
        document.querySelector<HTMLElement>("#session-navigation.open[role='dialog'][aria-modal='true']"));
    });
  } catch { /* The controller retains the error and exact recovery identity. */ }
}
export function TrashNotice({ state, controller, openSession, blocked, openDisabled = false }: {
  state: TrashState; controller: TrashController; openSession: (id: string) => void; blocked: boolean; openDisabled?: boolean;
}) {
  const notice = state.notice;
  if (!notice && !state.pending && !state.error) return null;
  return <div className="trash-notice">
    <div role="status">{state.pending ? "Updating sessions…" : notice && <>
      <strong>{notice.title || "New session"}</strong>{notice.status === "deleted" ? " moved to Trash. Your draft is kept." : notice.status === "restored" ? " restored to the session list." : " deleted forever."}
    </>}</div>
    {state.error && <p role="alert" className="error-text">{state.error}</p>}
    <div className="trash-notice-actions">
      {notice?.status === "deleted" && notice.item && <button disabled={blocked || !!state.pending || notice.item.purge_at * 1000 <= Date.now()}
        onClick={() => void restoreAndFocus(controller, notice.item!)}>Undo</button>}
      {notice?.status === "restored" && <button className="trash-open-restored" disabled={blocked || openDisabled || !!state.pending} onClick={() => openSession(notice.id)}>Open restored session</button>}
      {!state.open && <button disabled={!!state.pending} onClick={controller.show}>Trash</button>}
      <button disabled={!!state.pending} onClick={controller.dismiss} aria-label="Dismiss Trash notice">Dismiss</button>
    </div>
  </div>;
}

export function TrashDialog({ state, controller, permanent, openSession, blocked, openDisabled = false }: {
  state: TrashState; controller: TrashController; permanent: (item: TrashItem) => void;
  openSession: (id: string) => void; blocked: boolean; openDisabled?: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current;
    element?.showModal(); close.current?.focus();
    return () => {
      element?.close();
      restoreDeletionFocus(previous instanceof HTMLElement ? previous : null, document.getElementById("composer"),
        document.querySelector<HTMLElement>("#session-navigation.open[role='dialog'][aria-modal='true']"));
    };
  }, []);
  const pending = !!state.pending;
  const disabled = blocked || pending;
  return <dialog ref={dialog} className="approval-dialog trash-dialog" aria-labelledby="trash-title" aria-describedby="trash-description"
    onCancel={(event) => { event.preventDefault(); event.stopPropagation(); controller.close(); }}>
    <header className="approval-heading trash-heading"><div><h2 id="trash-title">Trash</h2>
      <p id="trash-description">Restore deleted sessions before their scheduled permanent deletion.</p></div>
      <button ref={close} className="trash-close" aria-label="Close Trash" disabled={pending} onClick={controller.close}>Close</button>
    </header>
    <div className="approval-body">
      <TrashNotice state={state} controller={controller} openSession={openSession} blocked={blocked} openDisabled={openDisabled} />
      <form className="trash-policy" onSubmit={(event) => { event.preventDefault(); void controller.save().catch(() => {}); }}>
        <label htmlFor="trash-retention">Keep deleted sessions for</label>
        <div><input id="trash-retention" type="number" inputMode="numeric" min={1} max={365} step={1} required
          disabled={!state.snapshot || disabled} value={state.draftDays} onChange={(event) => controller.editDays(event.target.value)} /> <span>days</span>
          <button disabled={!state.snapshot || disabled || state.draftDays === String(state.snapshot.retention_days)}>Save</button>
          <button type="button" disabled={pending} onClick={controller.cancelEdit}>Cancel changes</button></div>
        <p>1–365 days. Default: 30. Changes apply to future deletions; dates already shown below stay the same.</p>
      </form>
      <div className="trash-list-heading"><h3>Deleted sessions</h3>
        <button disabled={pending || state.loading} onClick={() => void controller.refresh()}>Refresh</button></div>
      {state.loading && <p role="status">Refreshing Trash…</p>}
      {state.snapshot && !state.snapshot.items.length && <p className="muted">Trash is empty.</p>}
      <ul className="trash-list">{state.snapshot?.items.map((item) => {
        const expired = item.purge_at * 1000 <= Date.now();
        return <li key={`${item.id}:${item.deletion_id}`}>
          <div><strong>{item.title || "New session"}</strong>
            <small>Deleted {trashDate(item.deleted_at)}</small>
            <small>{expired ? "Recovery expired; awaiting permanent cleanup." : `Deletes forever ${trashDate(item.purge_at)}`}</small></div>
          <div className="trash-row-actions"><button disabled={disabled || expired} onClick={() => void restoreAndFocus(controller, item)}>Restore</button>
            <button className="danger-action" disabled={disabled} onClick={() => permanent(item)}>Delete forever…</button></div>
        </li>;
      })}</ul>
      <p className="muted">Drafts remain in this browser. Restoring adds a session to the list; use Open to switch conversations.</p>
    </div>
  </dialog>;
}
