import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { deleteSessionFromView, SessionDeletion, type DeletionState } from "../../api/deletion.js";
import { purgeTrash, readTrash, restoreTrash, saveRetention, type TrashItem } from "../../api/trash.js";
import { TrashController, type TrashDependencies } from "./trashController.js";
import type { useSessions } from "./useSessions.js";
import type { useChatRun } from "../chat/useChatRun.js";
import type { useDictation } from "../dictation/useDictation.js";
import type { useOperations } from "../../app/useOperations.js";

type Options = {
  sessions: ReturnType<typeof useSessions>;
  chat: ReturnType<typeof useChatRun>;
  dictation: ReturnType<typeof useDictation>;
  operations: ReturnType<typeof useOperations>;
  busy: boolean;
  blocked: boolean;
};

/** Session membership changes own their controllers, drafts, and selection races. */
export function useSessionActions({ sessions, chat, dictation, operations, busy, blocked }: Options) {
  const token = sessions.config?.token || "";
  const [deletion, setDeletion] = useState<DeletionState>({ pending: false, error: "" });
  const removeAction = useRef(confirmPermanent);
  removeAction.current = confirmPermanent;
  const selection = useRef(sessions.currentSelection);
  selection.current = sessions.currentSelection;
  const [deletionController] = useState(() => new SessionDeletion(
    setDeletion, (id, item) => removeAction.current(id, item), () => selection.current().id,
  ));
  const dependencies: TrashDependencies = {
    read: (signal) => readTrash(token, signal),
    save: (revision, days) => saveRetention(token, revision, days),
    restore: (item) => restoreTrash(token, item),
    purge: async (item) => { const reply = await purgeTrash(token, item); chat.forgetSession(item.id, true); return reply; },
    restored: sessions.restored,
    softDelete: async (id) => {
      const reply = await removeSession(id, false);
      if (!reply.trash) throw new Error("Trash acknowledgement missing.");
      return reply.trash;
    },
    permanent: async (id) => { await removeSession(id, true); },
    mutate: trashMutation,
  };
  const latest = useRef(dependencies);
  latest.current = dependencies;
  const [trashController] = useState(() => new TrashController({
    read: (signal) => latest.current.read(signal), save: (revision, days) => latest.current.save(revision, days),
    restore: (item) => latest.current.restore(item), purge: (item) => latest.current.purge(item),
    restored: (reply) => latest.current.restored(reply), softDelete: (id) => latest.current.softDelete(id),
    permanent: (id) => latest.current.permanent(id), mutate: (action) => latest.current.mutate(action),
  }));
  const trash = useSyncExternalStore(trashController.subscribe, trashController.getSnapshot, trashController.getSnapshot);
  useEffect(() => { trashController.activate(); return () => trashController.dispose(); }, [trashController]);

  function canDeleteSession(id: string): boolean {
    const keepingReview = id !== sessions.currentSelection().id && dictation.controller.getSnapshot().phase === "review";
    return !!token && !operations.occupied() && !busy && !blocked && !trash.open &&
      (!dictation.controller.active || keepingReview);
  }

  async function trashMutation(action: () => Promise<void>) {
    if (!token || operations.occupied() || busy || blocked ||
        (dictation.controller.active && dictation.controller.getSnapshot().phase !== "review"))
      throw new Error("Finish the current operation before changing Trash.");
    await operations.run(action);
  }

  async function removeSession(id: string, permanent: boolean) {
    if (dictation.controller.active && id === sessions.currentSelection().id)
      throw new Error("Finish dictation review before deleting this session. Your draft is kept.");
    return deleteSessionFromView(id, {
      selection: sessions.currentSelection,
      remove: (root) => sessions.remove(root, permanent),
      drop: sessions.drop,
      forget: (root) => chat.forgetSession(root, permanent),
      replace: (snapshot) => {
        sessions.acceptDeletion(snapshot);
        chat.loadHistory(snapshot);
        chat.setStatus(permanent ? "Session deleted forever" : "Session moved to Trash. Restore it to recover its draft.");
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
    try { await trashController.move(session); return true; }
    catch { return false; /* The Trash notice owns the actionable error. */ }
  }

  return { deletion, deletionController, trash, trashController, canDeleteSession, softDelete };
}
