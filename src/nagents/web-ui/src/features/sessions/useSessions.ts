import { useRef, useState } from "react";
import { request, RequestError } from "../../api/client";
import { validSnapshot, type EventFrame } from "../../api/subscription";
import type { Bootstrap, Session, Snapshot } from "../../types";
import type { SettingsReply } from "../settings/types";
import { rootSessions } from "../channels/draft";
import { idle, sessionActivity } from "./activity";

export function useSessions() {
  const [config, setConfig] = useState<Bootstrap>();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState("");
  const selection = useRef("");
  const [active, setActive] = useState(idle);
  function selectRoot(id: string) { selection.current = id; setSessionId(id); }
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
    const data = (await (await request("bootstrap")).json()) as Bootstrap;
    setConfig(data); setActive({ id: data.active_run_id, sessionId: data.active_session_id || "", busy: !!data.active_run_id });
    let snapshot: Snapshot;
    try { snapshot = await snapshotResponse(await request("sessions", data.token)); }
    catch (cause) {
      // Older compatibility/background runs can still lock the default route.
      // Read a validated root directly without resuming the executing Harness.
      const root = selection.current || data.active_session_id;
      if (!(cause instanceof RequestError) || cause.status !== 409 || !root) throw cause;
      snapshot = await snapshotResponse(await request(`sessions/${encodeURIComponent(root)}`, data.token));
    }
    setSessions(rootSessions(snapshot.sessions));
    if (!selection.current || selection.current === snapshot.session_id) return accept(snapshot);
    // A reconnect never follows the Harness's current execution into another chat.
    return undefined;
  }
  async function select(id = ""): Promise<Snapshot | undefined> {
    if (!config) throw new Error("Reconnect to ngn before changing sessions.");
    if (id) {
      if (!sessions.some((session) => session.id === id)) throw new Error("Session unavailable. Refresh the connection.");
      // Keep the existing selected-session handshake for dictation's session and
      // settings-revision protections. The server-owned route selects the UI root
      // without moving the Harness underneath another chat's running producer.
      return accept(await snapshotResponse(await request("sessions/resume", config.token, { session_id: id })));
    }
    return accept(await snapshotResponse(await request("sessions/new", config.token, {})));
  }
  function receive(frame: EventFrame) {
    setActive((current) => sessionActivity(current, frame));
    if (frame.type === "snapshot" || frame.type === "sessions") {
      const list = frame.type === "snapshot" ? frame.snapshot.sessions : frame.sessions;
      setSessions(rootSessions(list));
    }
  }
  function acceptSettings(reply: SettingsReply) {
    setConfig((current) => current && { ...current, model: reply.values.model, agent: reply.values.agent,
      provider: reply.connection.provider, dictation: reply.dictation });
  }
  function acceptCredentials(bootstrap: Bootstrap) {
    // Credential-only recovery updates every HTTP consumer through config, while
    // leaving selected root, transcript, drafts and locally reviewed text intact.
    setConfig(bootstrap);
  }
  return {
    config, sessions, sessionId, globalRunId: active.id, globalBusy: active.busy, externalRun: active.sessionId !== sessionId ? active.id : "",
    activityOnly: false, connect, select, receive, acceptSettings, acceptCredentials,
  };
}
