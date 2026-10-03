import { useState, useSyncExternalStore, type SetStateAction } from "react";
import { SessionDrafts } from "./drafts.js";

export function useSessionDraft(sessionId: string) {
  const [drafts] = useState(() => new SessionDrafts());
  const read = () => drafts.get(sessionId);
  // Missing-root drafts are also visible through recovery UI, so changes to any
  // tab-local draft must refresh the owner even when the current text is unchanged.
  useSyncExternalStore(drafts.subscribe, drafts.version, drafts.version);
  const draft = read();

  function setPrompt(update: SetStateAction<string>) {
    drafts.set(sessionId, typeof update === "function" ? update(read().text) : update);
  }

  return { drafts, prompt: draft.text, setPrompt };
}
