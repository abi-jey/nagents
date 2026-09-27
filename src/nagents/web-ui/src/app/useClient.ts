import { useEffect, useState } from "react";
import { useChatRun } from "../features/chat/useChatRun";
import { useSessions } from "../features/sessions/useSessions";
import { useSettings } from "../features/settings/useSettings";
import { queuedMessageFailure } from "../api/messages";
import { useChannels } from "../features/channels/useChannels";
import { useContextStats } from "../features/context/useContext";
import { useSessionActions } from "../features/sessions/useSessionActions";
import { useUploads } from "../features/chat/useUploads";
import { useOperations } from "./useOperations";
import { availability } from "./availability";

export function useClient() {
  const sessions = useSessions();
  const chat = useChatRun(
    sessions.config?.token || "",
    sessions.sessionId,
    sessions.receive,
    sessions.acceptCredentials,
    () => void connect(),
  );
  const operations = useOperations();
  const { operating, error, setError } = operations;
  const [panel, setPanel] = useState<"none" | "tools" | "designer" | "live">("none");
  const busy = operating || !!chat.runId || sessions.globalBusy;

  // The UI rejects competing operations immediately, matching the backend's 409 policy.
  async function operate(action: () => Promise<void>): Promise<boolean> {
    return operations.operate(action);
  }

  const settings = useSettings({
    token: sessions.config?.token || "",
    blocked: busy || !!sessions.externalRun || !!chat.approval.pending,
    operate: (action) => busy ? Promise.resolve(false) : operate(action),
    accept: sessions.acceptSettings,
  });
  const channels = useChannels(sessions.config?.token || "", sessions.sessions, sessions.sessionId);
  const token = sessions.config?.token || "";
  const configuration = `${sessions.config?.provider}:${sessions.config?.model}:${sessions.config?.agent}:${settings.snapshot?.revision}`;
  const { uploads, uploadState } = useUploads(token, sessions.sessionId, configuration);
  const context = useContextStats(
    token,
    sessions.sessionId,
    { active: !!chat.runId, revision: chat.contextRevision, enabled: !sessions.activityOnly,
      configuration },
  );
  const membership = useSessionActions({ sessions, chat, operations, busy,
    blocked: channels.open || settings.open || panel !== "none" || !!chat.approval.pending });

  function access() {
    return availability({
      ready: !!token && !!sessions.sessionId,
      operating: operations.occupied(),
      running: !!chat.runId || sessions.globalBusy,
      modal: channels.open || settings.open || !!membership.deletion.target ||
        membership.trashController.getSnapshot().open || panel !== "none",
      approval: !!chat.approval.pending,
      uploadsReady: uploadState.items.every((item) => item.status === "ready"),
    });
  }

  async function connect() {
    await operate(async () => {
      chat.pause();
      chat.setStatus("Connecting to local harness");
      try {
        const snapshot = await sessions.connect();
        if (snapshot) {
          if (sessions.sessionId && !snapshot.sessions.some((session) => session.id === sessions.sessionId))
            chat.forgetSession(sessions.sessionId);
          chat.loadHistory(snapshot);
        }
        chat.reconnect();
      } catch (cause) {
        chat.setStatus("Disconnected");
        throw cause;
      }
    });
  }

  useEffect(() => {
    void connect();
  }, []);

  async function select(id = "") {
    if (id && id === sessions.sessionId) return true;
    if (!(id ? access().navigate : access().create)) return false;
    return operate(async () => {
      chat.pause();
      try {
        const snapshot = await sessions.select(id);
        if (snapshot) chat.loadHistory(snapshot);
      } finally { chat.reconnect(); }
    });
  }

  async function submit(value = chat.prompt) {
    if (
      !access().submit ||
      sessions.activityOnly ||
      (!value.trim() && !uploadState.items.length)
    )
      return;
    const failure = value.length > 32000
      ? "The draft exceeds 32,000 characters. Shorten it before sending; it is kept."
      : queuedMessageFailure(value, sessions.sessionId, uploadState.items.map((item) => item.id));
    if (failure) {
      setError(failure);
      return;
    }
    await operate(async () => {
      const ids = uploads.begin();
      let confirmed = false;
      try { await chat.submit(value, ids); confirmed = true; }
      finally { uploads.finish(ids, confirmed); }
    });
  }

  async function cancel() {
    try {
      await chat.cancel(chat.runId || sessions.externalRun);
      if (!chat.connected) await connect();
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Cancel failed. Reconnect to check the harness.",
      );
    }
  }

  return {
    ...membership,
    available: access(),
    panel,
    showTools: () => { if (access().tools) setPanel("tools"); },
    showDesigner: () => { if (access().designer) setPanel("designer"); },
    showLive: () => { if (access().live) setPanel("live"); },
    closePanel: () => { setPanel("none"); if (panel !== "live") void connect(); },
    dismissError: () => setError(""),
    sessions,
    context,
    chat,
    uploads,
    uploadState,
    settings,
    channels,
    busy,
    operating,
    error,
    connect,
    select,
    submit,
    cancel,
  };
}

export type Client = ReturnType<typeof useClient>;
