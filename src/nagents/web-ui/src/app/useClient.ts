import { useEffect, useRef, useState } from "react";
import { useChatRun } from "../features/chat/useChatRun";
import { useSessions } from "../features/sessions/useSessions";
import { useSettings } from "../features/settings/useSettings";
import { useDictation } from "../features/dictation/useDictation";
import { promptFailure } from "../features/dictation/draft";
import { recordingSupport } from "../features/dictation/browser";

export function useClient() {
  const sessions = useSessions();
  const chat = useChatRun(
    sessions.config?.token || "",
    sessions.sessionId,
    sessions.activityCursor,
    sessions.initialBackgroundRunId,
  );
  const [operating, setBusy] = useState(false);
  const busy = operating || !!chat.backgroundRunId;
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
    operate: (action) =>
      chat.backgroundRunId ? Promise.resolve(false) : operate(action),
    accept: sessions.acceptSettings,
  });

  async function connect() {
    dictation.controller.cancel("");
    await operate(async () => {
      chat.setStatus("Connecting to local harness");
      try {
        const snapshot = await sessions.connect();
        if (snapshot) chat.loadHistory(snapshot);
        else chat.setStatus("A run is active in another connection");
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
    if (!sessions.config || sessions.externalRun || chat.backgroundRunId)
      return false;
    return operate(async () => {
      chat.loadHistory(await sessions.select(id));
    });
  }

  async function submit(value = chat.prompt) {
    if (
      !sessions.config ||
      !sessions.sessionId ||
      sessions.activityOnly ||
      sessions.externalRun ||
      chat.backgroundRunId ||
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
      try {
        await chat.submit(value);
      } catch (cause) {
        sessions.invalidate();
        throw cause;
      }
      // Preserve partial output: navigation refresh does not reload saved history.
      await sessions.refresh();
    });
  }

  async function cancel() {
    try {
      await chat.cancel(chat.runId || sessions.externalRun);
      if (sessions.externalRun) await connect();
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
        chat.approval.pending || settings.open || !sessions.config?.dictation ||
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
    dictation,
    startDictation,
    insertDictation,
    busy,
    error,
    connect,
    select,
    submit,
    cancel,
  };
}
