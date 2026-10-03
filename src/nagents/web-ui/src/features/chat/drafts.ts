export type Draft = { text: string; revision: number };
export type OrphanedDraft = Draft & { sessionId: string };
const empty: Draft = { text: "", revision: 0 };

/** Tab-local drafts belong to a conversation, including while it is in Trash. */
export class SessionDrafts {
  private drafts = new Map<string, Draft>();
  private listeners = new Set<() => void>();
  private revision = 0;

  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  get = (sessionId: string): Draft => this.drafts.get(sessionId) || empty;
  version = (): number => this.revision;

  orphaned(available: readonly string[]): OrphanedDraft[] {
    const known = new Set(available);
    return [...this.drafts].flatMap(([sessionId, draft]) => draft.text.length && !known.has(sessionId)
      ? [{ ...draft, sessionId }] : []);
  }

  moveToEmpty(sourceId: string, destinationId: string): boolean {
    const source = this.get(sourceId);
    if (!source.text.length) return false;
    if (sourceId === destinationId) return true;
    if (this.get(destinationId).text.length) return false;
    // One synchronous commit preserves late typing and never overwrites another
    // draft. A new revision also fences acknowledgements from either old root.
    this.drafts.set(destinationId, { text: source.text, revision: ++this.revision });
    this.drafts.delete(sourceId);
    this.listeners.forEach((listener) => listener());
    return true;
  }

  set(sessionId: string, text: string) {
    this.drafts.set(sessionId, { text, revision: ++this.revision });
    this.listeners.forEach((listener) => listener());
  }

  submitted(sessionId: string, submitted: Draft, prompt: string) {
    const current = this.get(sessionId);
    // A late acknowledgement must not erase typing, even after away-and-back navigation.
    if (current.revision === submitted.revision && current.text === prompt) this.set(sessionId, "");
  }

  forget(sessionId: string) {
    if (!this.drafts.delete(sessionId)) return;
    this.revision++;
    this.listeners.forEach((listener) => listener());
  }
}
