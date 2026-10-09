import { useEffect, useRef, useState } from "react";
import { request } from "../../api/client.js";
import { readSessionHistory } from "../../api/sessionHistory.js";
import { validSnapshot, type EventFrame } from "../../api/subscription.js";
import type { Bootstrap, Session, Snapshot } from "../../types.js";
import type { SettingsReply } from "../settings/types.js";
import { rootSessions } from "../channels/draft.js";
import { idle, sessionActivity } from "./activity.js";
import { deleteSession, type DeletedSnapshot, type DeletionSelection } from "../../api/deletion.js";
import type { RestoreReply } from "../../api/trash.js";

export function useSessions() {
  const [config, setConfig] = useState<Bootstrap>();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState("");
  const selection = useRef("");
  const selectionRevision = useRef(0);
  const [active, setActive] = useState(idle);
  const connection = useRef<AbortController>(undefined);
  const mounted = useRef(false);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      const current = connection.current;
      // React's development effect probe immediately remounts this same owner;
      // a real unmount still cancels before another fetch/timer can run.
      queueMicrotask(() => { if (!mounted.current) current?.abort(); });
    };
  }, []);
  function selectRoot(id: string) { selection.current = id; selectionRevision.current++; setSessionId(id); }
  function currentSelection(): DeletionSelection { return { id: selection.current, revision: selectionRevision.current }; }
  function accept(snapshot: Snapshot): Snapshot {
    setSessions(rootSessions(snapshot.sessions)); selectRoot(snapshot.session_id);
    setActive((current) => sessionActivity(current, { type: "snapshot", session_id: snapshot.session_id, snapshot, cursor: 0, epoch: "http" }));
    return snapshot;
  }
  async function snapshotResponse(response: Response): Promise<Snapshot> {
    const data: unknown = await response.json();
    if (!validSnapshot(data)) throw new Error("Invalid session history. Reconnect to try again.");
    return data;
  }
  async function connect(): Promise<Snapshot | undefined> {
    connection.current?.abort();
    const controller = new AbortController();
    connection.current = controller;
    const data = (await (await request("bootstrap", "", undefined, controller.signal)).json()) as Bootstrap;
    controller.signal.throwIfAborted();
    setConfig(data); setActive({ id: data.active_run_id, sessionId: data.active_session_id || "", busy: !!data.active_run_id });
    const snapshot = await readSessionHistory(data.token, controller.signal, selection.current || data.active_session_id || "");
    controller.signal.throwIfAborted();
    setSessions(rootSessions(snapshot.sessions));
    if (!selection.current || selection.current === snapshot.session_id ||
        !snapshot.sessions.some((session) => session.id === selection.current)) return accept(snapshot);
    // A reconnect never follows the Harness's current execution into another chat.
    return undefined;
  }
  async function select(id = ""): Promise<Snapshot | undefined> {
    if (!config) throw new Error("Reconnect to ngn before changing sessions.");
    if (id) {
      if (!sessions.some((session) => session.id === id)) throw new Error("Session unavailable. Refresh the connection.");
      // The server-owned route selects the UI root
      // without moving the Harness underneath another chat's running producer.
      return accept(await snapshotResponse(await request("sessions/resume", config.token, { session_id: id })));
    }
    return accept(await snapshotResponse(await request("sessions/new", config.token, {})));
  }
  async function remove(id: string, permanent = false): Promise<DeletedSnapshot> {
    if (!config) throw new Error("Reconnect to ngn before deleting a session.");
    return deleteSession(config.token, id, permanent);
  }
  function restored(reply: RestoreReply) {
    const item = reply.sessions.find((session) => session.id === reply.restored_session_id);
    if (item) setSessions((current) => [item, ...current.filter((session) => session.id !== item.id)]);
  }
  function drop(id: string) { setSessions((current) => current.filter((session) => session.id !== id)); }
  function receive(frame: EventFrame) {
    setActive((current) => sessionActivity(current, frame));
    if (frame.type === "snapshot" || frame.type === "sessions") {
      const list = frame.type === "snapshot" ? frame.snapshot.sessions : frame.sessions;
      setSessions(rootSessions(list));
    }
  }
  function acceptSettings(reply: SettingsReply) {
    setConfig((current) => current && { ...current, model: reply.effective_model || reply.values.model, agent: reply.values.agent,
      provider: reply.connection.provider });
  }
  function acceptCredentials(bootstrap: Bootstrap) {
    // Credential-only recovery updates every HTTP consumer through config, while
    // leaving selected root, transcript, drafts and locally reviewed text intact.
    setConfig(bootstrap);
  }
  return {
    config, sessions: sessions.map<Session>((session) => ({ ...session,
      active_run_id: active.busy && active.sessionId === session.id ? active.id : "",
    })), sessionId, activeSessionId: active.sessionId,
    globalRunId: active.id, globalBusy: active.busy, externalRun: active.sessionId !== sessionId ? active.id : "",
    activityOnly: false, connect, select, remove, restored, drop, currentSelection, acceptDeletion: accept, receive, acceptSettings, acceptCredentials,
  };
}
