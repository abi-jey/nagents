import type { OrphanedDraft } from "./drafts.js";

export function DraftRecoveryNotice({ drafts, disabled, recover }: {
  drafts: readonly OrphanedDraft[];
  disabled: boolean;
  recover(id: string): void;
}) {
  if (!drafts.length) return null;
  return <section className="draft-recovery" aria-label="Unsent draft recovery">
    <p role="status"><strong>{drafts.length === 1 ? "Your unsent draft is kept." : `${drafts.length} unsent drafts are kept.`}</strong> The original conversation is no longer in this list. Recover into a blank conversation, or expand the text to copy it.</p>
    {drafts.map((draft, index) => <div key={draft.sessionId} className="draft-recovery-item">
      <details>
        <summary>Unsent draft {index + 1}<span>{draft.text.replace(/\s+/g, " ").trim().slice(0, 100) || `${draft.text.length} characters`}</span></summary>
        <textarea readOnly value={draft.text} aria-label={`Original unsent draft ${index + 1}`} rows={5} />
      </details>
      <button type="button" disabled={disabled} onClick={() => recover(draft.sessionId)} aria-label={`Recover draft ${index + 1}`}>Recover draft</button>
    </div>)}
  </section>;
}
