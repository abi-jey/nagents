export type Draft = { text: string; revision: number };
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
    this.drafts.delete(sessionId);
    this.listeners.forEach((listener) => listener());
  }
}
