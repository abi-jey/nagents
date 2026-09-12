import { useEffect, useRef, useState } from "react";
import { RequestError } from "../../api/client.js";
import type { Session } from "../../types.js";
import { buildSave, makeDraft } from "./draft.js";
import { channelRequest } from "./transport.js";
import type { Catalog, ChannelDraft } from "./types.js";

export function useChannels(token: string, sessions: Session[], selected: string) {
  const [open, setOpen] = useState(false);
  const [catalog, setCatalog] = useState<Catalog>();
  const [draft, setDraft] = useState<ChannelDraft>();
  const [editing, setEditing] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [conflict, setConflict] = useState(false);
  const controller = useRef<AbortController | undefined>(undefined);
  useEffect(() => () => controller.current?.abort(), [token]);
  async function operation(action: (signal: AbortSignal) => Promise<void>) {
    if (controller.current) return;
    const request = new AbortController();
    controller.current = request;
    setPending(true); setError(""); setNotice("");
    try { await action(request.signal); }
    catch (cause) {
      if (!request.signal.aborted) {
        setError(cause instanceof RequestError ? cause.message : "Channel operation was not confirmed. Refresh and check the saved state before retrying.");
        if (cause instanceof RequestError && cause.status === 409) setConflict(true);
      }
    } finally {
      if (controller.current === request) { controller.current = undefined; setPending(false); }
    }
  }
  function choose(id = "") {
    if (!catalog || pending) return;
    const connection = catalog.connections.find((item) => item.id === id);
    setEditing(connection?.id || ""); setDraft(makeDraft(catalog, selected, connection));
    setErrors({}); setError(""); setNotice("");
  }
  function update(update: Partial<ChannelDraft>) { setDraft((current) => current && { ...current, ...update }); setErrors({}); }
  async function refresh() {
    await operation(async (signal) => {
      const next = await channelRequest(token, signal, "refresh");
      setCatalog(next); setConflict(false);
      setDraft((current) => current || makeDraft(next, selected));
      setNotice("Installed plugins refreshed. Your draft is kept; review the saved connection before saving over newer changes.");
    });
  }
  function show() {
    setOpen(true); setCatalog(undefined); setDraft(undefined); setEditing(""); setErrors({}); setConflict(false);
    void operation(async (signal) => {
      const next = await channelRequest(token, signal, "load");
      setCatalog(next); setDraft(makeDraft(next, selected));
    });
  }
  async function save() {
    if (!catalog || !draft || conflict || pending) return;
    const result = buildSave(catalog, draft, sessions);
    if (!editing && catalog.connections.some((item) => item.id === draft.id)) result.errors.id = "That instance ID already exists. Select it to edit its configuration.";
    setErrors(result.errors);
    if (!result.payload || Object.values(result.errors).some(Boolean)) return;
    await operation(async (signal) => {
      const next = await channelRequest(token, signal, "save", draft.id, result.payload);
      setCatalog(next); setEditing(draft.id);
      setDraft(makeDraft(next, selected, next.connections.find((item) => item.id === draft.id)));
      setNotice("Channel configuration saved. Secret inputs have been cleared.");
    });
  }
  async function remove() {
    if (!catalog || !editing || conflict || pending) return;
    await operation(async (signal) => {
      const next = await channelRequest(token, signal, "delete", editing, { revision: catalog.revision });
      setCatalog(next); setEditing(""); setDraft(makeDraft(next, selected)); setErrors({});
      setNotice("Channel instance deleted.");
    });
  }
  return { open, catalog, draft, editing, pending, error, notice, errors, conflict, choose, update, refresh, show, save, remove,
    close: () => { if (!pending) { setOpen(false); setDraft(undefined); } } };
}
