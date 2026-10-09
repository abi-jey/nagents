import { useEffect, useRef, useState } from "react";
import { changeFolder, readFolders, type ChatFolder, type ChatFolders } from "../../api/sessionGroups.js";
import { RequestError } from "../../api/client.js";

/** Folder edits never change the selected chat or its execution owner. */
export function useChatFolders(token: string, sessionIds: string[]) {
  const [value, setValue] = useState<ChatFolders>();
  const current = useRef<ChatFolders>(undefined);
  const [error, setError] = useState("");
  const [pending, setPending] = useState(false);
  const occupied = useRef(false);
  const mounted = useRef(false);
  const latestToken = useRef(token); latestToken.current = token;
  const read = useRef<AbortController>(undefined);
  const membership = [...sessionIds].sort().join(",");
  async function refresh() {
    read.current?.abort();
    if (!token) return;
    const controller = new AbortController(); read.current = controller;
    try {
      const next = await readFolders(token, controller.signal);
      if (!controller.signal.aborted && mounted.current && latestToken.current === token) {
        current.current = next; setValue(next);
      }
    } catch (cause) {
      if (!controller.signal.aborted && mounted.current)
        setError(cause instanceof Error ? cause.message : "Could not load chat folders.");
    }
  }
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; read.current?.abort(); };
  }, []);
  useEffect(() => {
    current.current = undefined; setValue(undefined); setError("");
    void refresh();
    return () => read.current?.abort();
  }, [token]);
  useEffect(() => {
    if (!occupied.current) void refresh();
  }, [membership]);
  useEffect(() => {
    const focus = () => { if (!occupied.current) void refresh(); };
    window.addEventListener("focus", focus);
    return () => window.removeEventListener("focus", focus);
  }, [token]);
  async function mutate(action: "create" | "update" | "move" | "remove" | "reparent", fields: object): Promise<boolean> {
    if (!token || !current.current || occupied.current) return false;
    occupied.current = true; setPending(true); setError(""); read.current?.abort();
    try {
      const next = await changeFolder(token, action, { ...fields, revision: current.current.revision });
      if (!mounted.current || latestToken.current !== token) return false;
      current.current = next; setValue(next);
      return true;
    } catch (cause) {
      if (mounted.current && latestToken.current === token) {
        setError(cause instanceof Error ? cause.message : "Could not update chat folders.");
        if (cause instanceof RequestError && [404, 409].includes(cause.status)) await refresh();
      }
      return false;
    } finally {
      occupied.current = false;
      if (mounted.current) setPending(false);
    }
  }
  return {
    value, error, pending, ready: !!token && !!value,
    clearError: () => setError(""), refresh,
    create: (name: string, parentId = "") => mutate("create", { name, parent_id: parentId }),
    update: (folder: ChatFolder, name?: string, collapsed?: boolean) => {
      const latest = current.current?.groups.find(item => item.id === folder.id) || folder;
      return mutate("update", { group_id: folder.id, name: name ?? latest.name, collapsed: collapsed ?? latest.collapsed });
    },
    remove: (folder: ChatFolder) => mutate("remove", { group_id: folder.id }),
    reparent: (folder: ChatFolder, parentId: string) => mutate("reparent", { group_id: folder.id, parent_id: parentId }),
    move: (sessionId: string, folderId: string) => mutate("move", { session_id: sessionId, group_id: folderId }),
  };
}
export type ChatFolderActions = ReturnType<typeof useChatFolders>;
