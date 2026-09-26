import { useState, useSyncExternalStore, type SetStateAction } from "react";
import { SessionDrafts } from "./drafts.js";

export function useSessionDraft(sessionId: string) {
  const [drafts] = useState(() => new SessionDrafts());
  const read = () => drafts.get(sessionId);
  const draft = useSyncExternalStore(drafts.subscribe, read, read);

  function setPrompt(update: SetStateAction<string>) {
    drafts.set(sessionId, typeof update === "function" ? update(read().text) : update);
  }

  return { drafts, prompt: draft.text, setPrompt };
}
