import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useChatRun } from "../features/chat/useChatRun";
import { useSessions } from "../features/sessions/useSessions";
import { useSettings } from "../features/settings/useSettings";
import { useDictation } from "../features/dictation/useDictation";
import { promptFailure } from "../features/dictation/draft";
import { recordingSupport } from "../features/dictation/browser";
import { useChannels } from "../features/channels/useChannels";
import { deleteSessionFromView, SessionDeletion, type DeletionState } from "../api/deletion";
import { purgeTrash, readTrash, restoreTrash, saveRetention, type TrashItem } from "../api/trash";
import { TrashController, type TrashDependencies } from "../features/sessions/trashController";

export function useClient() {
  const sessions = useSessions();
  const chat = useChatRun(
    sessions.config?.token || "",
    sessions.sessionId,
    sessions.receive,
    sessions.acceptCredentials,
    () => void connect(),
  );
  const [operating, setBusy] = useState(false);
  const busy = operating || !!chat.runId || sessions.globalBusy;
  const [error, setError] = useState("");
  const occupied = useRef(false);
  const [deletion, setDeletion] = useState<DeletionState>({ pending: false, error: "" });
  const removeAction = useRef(confirmPermanent);
  removeAction.current = confirmPermanent;
  const [deletionController] = useState(() => new SessionDeletion(setDeletion, (id, item) => removeAction.current(id, item), () => sessions.currentSelection().id));
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
  const token = sessions.config?.token || "";
  const dependencies: TrashDependencies = {
    read: (signal) => readTrash(token, signal), save: (revision, days) => saveRetention(token, revision, days),
    restore: (item) => restoreTrash(token, item), purge: (item) => purgeTrash(token, item), restored: sessions.restored,
    softDelete: async (id) => { const reply = await removeSession(id, false); if (!reply.trash) throw new Error("Trash acknowledgement missing."); return reply.trash; },
    permanent: async (id) => { await removeSession(id, true); }, mutate: trashMutation,
  };
  const trashDeps = useRef(dependencies);
  trashDeps.current = dependencies;
  const [trashController] = useState(() => new TrashController({
    read: (signal) => trashDeps.current.read(signal), save: (revision, days) => trashDeps.current.save(revision, days),
    restore: (item) => trashDeps.current.restore(item), purge: (item) => trashDeps.current.purge(item),
    restored: (reply) => trashDeps.current.restored(reply), softDelete: (id) => trashDeps.current.softDelete(id),
    permanent: (id) => trashDeps.current.permanent(id), mutate: (action) => trashDeps.current.mutate(action),
  }));
  const trash = useSyncExternalStore(trashController.subscribe, trashController.getSnapshot, trashController.getSnapshot);
  useEffect(() => { trashController.activate(); return () => trashController.dispose(); }, [trashController]);

  async function connect() {
    dictation.controller.cancel("");
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
    if (!sessions.config || (!id && busy) || channels.open || settings.open || deletion.target || trashController.getSnapshot().open)
      return false;
    return operate(async () => {
      chat.pause();
      try {
        const snapshot = await sessions.select(id);
        if (snapshot) chat.loadHistory(snapshot);
      } finally { chat.reconnect(); }
    });
  }

  function canDeleteSession(id: string): boolean {
    const keepingReview = id !== sessions.currentSelection().id && dictation.controller.getSnapshot().phase === "review";
    return !!sessions.config && !occupied.current && !busy && !channels.open && !settings.open && !trash.open &&
      (!dictation.controller.active || keepingReview);
  }

  async function trashMutation(action: () => Promise<void>): Promise<void> {
    if (!sessions.config || occupied.current || busy || channels.open || settings.open ||
        (dictation.controller.active && dictation.controller.getSnapshot().phase !== "review"))
      throw new Error("Finish the current operation before changing Trash.");
    occupied.current = true; setBusy(true); setError("");
    try { await action(); }
    finally { occupied.current = false; setBusy(false); }
  }
  async function removeSession(id: string, permanent: boolean) {
    if (dictation.controller.active && id === sessions.currentSelection().id)
      throw new Error("Finish dictation review before deleting this session. Your draft is kept.");
    return deleteSessionFromView(id, {
      selection: sessions.currentSelection,
      remove: (root) => sessions.remove(root, permanent),
      drop: sessions.drop,
      forget: chat.forgetSession,
      replace: (snapshot) => {
        sessions.acceptDeletion(snapshot);
        chat.loadHistory(snapshot);
        chat.setStatus(permanent ? "Session deleted forever" : "Session moved to Trash. Your draft is kept.");
      },
      pause: chat.pause,
      reconnect: chat.reconnect,
    }, permanent);
  }
  async function confirmPermanent(id: string, item?: TrashItem): Promise<boolean> {
    await trashController.permanent(id, item);
    return true;
  }
  async function softDelete(session: typeof sessions.sessions[number]) {
    if (!canDeleteSession(session.id)) return false;
    try {
      await trashController.move(session);
      return true;
    } catch { return false; /* The persistent Trash notice owns the actionable error. */ }
  }

  async function submit(value = chat.prompt) {
    if (
      !sessions.config ||
      !sessions.sessionId ||
      sessions.activityOnly ||
      channels.open || settings.open || deletion.target || trash.open ||
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
         chat.approval.pending || settings.open || channels.open || trash.open || !sessions.config?.dictation ||
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
    deletion,
    deletionController,
    canDeleteSession,
    softDelete,
    trash,
    trashController,
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
