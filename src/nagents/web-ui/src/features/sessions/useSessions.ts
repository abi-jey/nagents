import { useState } from "react";
import { request } from "../../api/client";
import type { Bootstrap, Session, Snapshot } from "../../types";

export function useSessions() {
  const [config, setConfig] = useState<Bootstrap>();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [externalRun, setExternalRun] = useState("");

  function accept(snapshot: Snapshot): Snapshot {
    setSessions(snapshot.sessions);
    setSessionId(snapshot.session_id);
    return snapshot;
  }

  async function connect(): Promise<Snapshot | undefined> {
    setSessionId("");
    const data = (await (await request("bootstrap")).json()) as Bootstrap;
    setConfig(data);
    setExternalRun(data.active_run_id);
    if (data.active_run_id) return;
    return accept(
      (await (await request("sessions", data.token)).json()) as Snapshot,
    );
  }

  async function select(id = ""): Promise<Snapshot> {
    if (!config) throw new Error("Reconnect to ngn before changing sessions.");
    const response = await request(
      id ? "sessions/resume" : "sessions/new",
      config.token,
      id ? { session_id: id } : {},
    );
    return accept((await response.json()) as Snapshot);
  }

  async function refresh(): Promise<void> {
    if (!config) return;
    const snapshot = (await (
      await request("sessions", config.token)
    ).json()) as Snapshot;
    setSessions(snapshot.sessions);
  }

  return {
    config,
    sessions,
    sessionId,
    externalRun,
    connect,
    select,
    refresh,
    invalidate: () => setSessionId(""),
  };
}
