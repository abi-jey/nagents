import type { Snapshot } from "../../types.js";
import type { SessionDrafts } from "./drafts.js";

interface RecoveryDependencies {
  drafts: SessionDrafts;
  openBlank(): Promise<Snapshot | undefined>;
  selected(): string;
  load(snapshot: Snapshot): void;
}

/** Recover only into the blank root acknowledged by New; never submit text. */
export async function recoverOrphanDraft(sourceId: string, deps: RecoveryDependencies): Promise<void> {
  if (!deps.drafts.get(sourceId).text.length)
    throw new Error("This draft is no longer available. No text was moved.");
  const snapshot = await deps.openBlank();
  if (!snapshot || !snapshot.session_id || deps.selected() !== snapshot.session_id ||
      !snapshot.sessions.some((session) => session.id === snapshot.session_id && !session.parent_session_id))
    throw new Error("The blank conversation could not be confirmed. Your original draft is kept below; try recovery again.");
  deps.load(snapshot);
  if (snapshot.history.length || snapshot.retained_tasks.length || snapshot.active_run)
    throw new Error("This conversation is not blank. Your original draft is kept below; try recovery again.");
  if (!deps.drafts.get(sourceId).text.length)
    throw new Error("This draft changed while opening the conversation. No text was moved.");
  if (!deps.drafts.moveToEmpty(sourceId, snapshot.session_id))
    throw new Error("This conversation already has an unsent draft. Both drafts are kept. Select a conversation with saved messages, then recover again to open a new blank conversation. You can also copy the original text below.");
}
