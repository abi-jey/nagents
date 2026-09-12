import { useState } from "react";
import { request } from "../../api/client";
import type { Bootstrap, Session, Snapshot } from "../../types";
import type { SettingsReply } from "../settings/types";

export function useSessions() {
  const [config, setConfig] = useState<Bootstrap>();
  const [sessions, setSessions] = useState<Session[]>([]);
  const [sessionId, setSessionId] = useState("");
  const [externalRun, setExternalRun] = useState("");
  const [activityCursor, setActivityCursor] = useState(0);
  const [activityOnly, setActivityOnly] = useState(false);
  const [initialBackgroundRunId, setInitialBackgroundRunId] = useState("");

  function accept(snapshot: Snapshot): Snapshot {
    setSessions(snapshot.sessions);
    setSessionId(snapshot.session_id);
    setActivityCursor(snapshot.activity_cursor || 0);
    setActivityOnly(false);
    setInitialBackgroundRunId("");
    return snapshot;
  }

  async function connect(): Promise<Snapshot | undefined> {
    setSessionId("");
    const data = (await (await request("bootstrap")).json()) as Bootstrap;
    setConfig(data);
    if (
      data.active_run_id &&
      data.active_run_background &&
      data.active_session_id
    ) {
      setExternalRun("");
      const snapshot = accept({
        session_id: data.active_session_id,
        sessions: [],
        history: [],
        retained_tasks: [],
        activity_cursor: 0,
      });
      setActivityOnly(true);
      setInitialBackgroundRunId(data.active_run_id);
      return snapshot;
    }
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

  function acceptSettings(reply: SettingsReply) {
    // Settings must not reload history, switch sessions, or clear partial output.
    setConfig(
      (current) =>
        current && {
          ...current,
          model: reply.values.model,
          agent: reply.values.agent,
          provider: reply.connection.provider,
          dictation: reply.dictation,
        },
    );
  }

  return {
    config,
    sessions,
    sessionId,
    externalRun,
    activityCursor,
    activityOnly,
    initialBackgroundRunId,
    connect,
    select,
    refresh,
    acceptSettings,
    invalidate: () => setSessionId(""),
  };
}
