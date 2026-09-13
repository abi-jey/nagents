import { useEffect, useRef, useState } from "react";
import { useChatRun } from "../features/chat/useChatRun";
import { useSessions } from "../features/sessions/useSessions";
import { useSettings } from "../features/settings/useSettings";
import { useDictation } from "../features/dictation/useDictation";
import { promptFailure } from "../features/dictation/draft";
import { recordingSupport } from "../features/dictation/browser";
import { useChannels } from "../features/channels/useChannels";

export function useClient() {
  const sessions = useSessions();
  const chat = useChatRun(
    sessions.config?.token || "",
    sessions.sessionId,
    sessions.receive,
    sessions.acceptCredentials,
  );
  const [operating, setBusy] = useState(false);
  const busy = operating || !!chat.runId || sessions.globalBusy;
  const [error, setError] = useState("");
  const occupied = useRef(false);
  const dictation = useDictation(
    sessions.config?.token || "",
    sessions.sessionId,
    busy || !!sessions.externalRun || !!chat.approval.pending || sessions.activityOnly,
  );

  // The UI rejects competing operations immediately, matching the backend's 409 policy.
  async function operate(action: () => Promise<void>): Promise<boolean> {
    if (occupied.current || dictation.controller.active) return false;
    occupied.current = true;
    setBusy(true);
    setError("");
    try {
      await action();
      return true;
    } catch (cause) {
      setError(
        cause instanceof Error
          ? cause.message
          : "Local request failed. No operation was retried.",
      );
      return false;
    } finally {
      occupied.current = false;
      setBusy(false);
    }
  }

  const settings = useSettings({
    token: sessions.config?.token || "",
    blocked: busy || !!sessions.externalRun || !!chat.approval.pending || dictation.unfinished,
    operate: (action) => busy ? Promise.resolve(false) : operate(action),
    accept: sessions.acceptSettings,
  });
  const channels = useChannels(sessions.config?.token || "", sessions.sessions, sessions.sessionId);

  async function connect() {
    dictation.controller.cancel("");
    await operate(async () => {
      chat.pause();
      chat.setStatus("Connecting to local harness");
      try {
        const snapshot = await sessions.connect();
        if (snapshot) chat.loadHistory(snapshot);
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
    if (!sessions.config || (!id && busy) || channels.open || settings.open)
      return false;
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
      !sessions.config ||
      !sessions.sessionId ||
      sessions.activityOnly ||
      channels.open || settings.open ||
      dictation.controller.active ||
      !value.trim()
    )
      return;
    const failure = promptFailure(value, sessions.sessionId);
    if (failure) {
      setError(failure);
      return;
    }
    await operate(async () => {
      await chat.submit(value);
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

  function startDictation() {
    if (occupied.current || busy || sessions.externalRun || sessions.activityOnly ||
        chat.approval.pending || settings.open || channels.open || !sessions.config?.dictation ||
        !sessions.sessionId || recordingSupport()) return;
    void dictation.controller.start({
      token: sessions.config.token,
      sessionId: sessions.sessionId,
      config: sessions.config.dictation,
    });
  }

  function insertDictation() {
    return dictation.controller.insert(chat.insertDictation);
  }

  return {
    sessions,
    chat,
    settings,
    channels,
    dictation,
    startDictation,
    insertDictation,
    busy,
    operating,
    error,
    connect,
    select,
    submit,
    cancel,
  };
}
