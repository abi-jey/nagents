import { useCallback, useEffect, useRef, useState } from "react";
import type { EventFrame } from "../../api/subscription.js";
import { readSessionAgent, saveSessionAgent, SessionAgentController, type SessionAgentSelection } from "./sessionAgent.js";

export function useSessionAgent(token: string, sessionId: string) {
  const key = `${token}:${sessionId}`;
  const [state, setState] = useState<{ key: string; selection?: SessionAgentSelection; loading: boolean; saving: boolean; error: string }>({
    key: "", loading: false, saving: false, error: "",
  });
  const [open, setOpen] = useState(false);
  const owner = useRef<SessionAgentController>(undefined);
  useEffect(() => {
    setOpen(false);
    setState({ key, loading: !!token && !!sessionId, saving: false, error: "" });
    if (!token || !sessionId) return;
    const controller = new SessionAgentController({
      sessionId,
      read: signal => readSessionAgent(token, sessionId, signal),
      save: (revision, agent, signal) => saveSessionAgent(token, sessionId, revision, agent, signal),
      accept: selection => setState(value => ({ ...value, key, selection })),
      error: error => setState(value => ({ ...value, key, error })),
      loading: loading => setState(value => ({ ...value, key, loading })),
      saving: saving => setState(value => ({ ...value, key, saving })),
    });
    owner.current = controller;
    void controller.refresh();
    return () => { controller.dispose(); if (owner.current === controller) owner.current = undefined; };
  }, [key, token, sessionId]);
  const refresh = useCallback(() => { void owner.current?.refresh(); }, []);
  const show = useCallback(() => { setOpen(true); void owner.current?.refresh(true); }, []);
  const choose = useCallback((name: string) => owner.current?.choose(name) || Promise.resolve(false), []);
  function receive(frame: EventFrame) {
    if ((frame.type !== "snapshot" && frame.type !== "event") || frame.session_id !== sessionId) return;
    if (frame.type === "snapshot") owner.current?.accept(frame.snapshot.agent_selection);
    else if (frame.record.event === "agent_selection_changed") owner.current?.accept(frame.record.agent_selection);
    else if (["run_started", "run_finished", "agent_changed"].includes(frame.record.event)) refresh();
  }
  const current = state.key === key ? state : { loading: !!token && !!sessionId, saving: false, error: "", selection: undefined };
  return { ...current, open, show, close: () => setOpen(false), refresh, choose, receive,
    identity: current.selection?.active || current.selection?.effective };
}
