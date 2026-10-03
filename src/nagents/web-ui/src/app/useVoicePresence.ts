import { useCallback, useState } from "react";

// A visible voice composer can survive a credentials refresh. Permission to
// start a new call cannot: the keyed child consumes each explicit launch once.
export function useVoicePresence() {
  const [state, setState] = useState({ open: false, intent: "idle" as "idle" | "start" | "settings", token: "", sessionId: "", request: 0 });
  const show = useCallback((intent: "start" | "settings", token: string, sessionId: string) => {
    setState(current => ({ open: true, intent, token, sessionId, request: current.request + 1 }));
  }, []);
  const close = useCallback(() => { setState(current => ({ ...current, open: false, intent: "idle", token: "", sessionId: "" })); }, []);
  const consumeStart = useCallback((request: number) => {
    setState(current => current.intent === "start" && current.request === request
      ? { ...current, intent: "idle" } : current);
  }, []);
  const shouldStart = (token: string, sessionId: string) => state.open && state.intent === "start"
    && state.token === token && state.sessionId === sessionId;
  return { open: state.open, intent: state.intent, request: state.request, show, close, consumeStart, shouldStart };
}
