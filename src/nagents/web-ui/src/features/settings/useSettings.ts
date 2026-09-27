import { useEffect, useRef, useState } from "react";
import {
  createDraft,
  parseDraft,
  selectProfile,
  type DraftErrors,
  type SettingsDraft,
} from "./draft.js";
import {
  readSettings,
  resetSettings,
  saveSettings,
  settingsFailure,
} from "./transport.js";
import type { SettingsReply } from "./types.js";
import type { SettingsScope } from "./transport.js";

export function useSettings({
  token,
  blocked,
  operate,
  accept,
}: {
  token: string;
  blocked: boolean;
  operate: (action: () => Promise<void>) => Promise<boolean>;
  accept: (reply: SettingsReply) => void;
}) {
  const [open, setOpen] = useState(false);
  const [scope, setScope] = useState<SettingsScope>("workspace");
  const scopeRef = useRef<SettingsScope>("workspace");
  const [snapshot, setSnapshot] = useState<SettingsReply>();
  const [draft, setDraft] = useState<SettingsDraft>();
  const [errors, setErrors] = useState<DraftErrors>({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const reading = useRef<AbortController | null>(null);
  const writing = useRef(false);

  useEffect(() => () => reading.current?.abort(), []);

  const dirty =
    (!!snapshot &&
      !!draft &&
      Object.entries(createDraft(snapshot.values)).some(
        ([key, value]) => draft[key as keyof SettingsDraft] !== value,
      ));
  const disabled = blocked || loading || pending;

  function receive(reply: SettingsReply) {
    setSnapshot(reply);
    setDraft(createDraft(reply.values));
    setErrors({});
    setError("");
    setNeedsRefresh(false);
    if (scopeRef.current === "workspace") accept(reply);
  }

  async function refresh() {
    if (writing.current || blocked) return;
    reading.current?.abort();
    const controller = new AbortController();
    reading.current = controller;
    setLoading(true);
    setNotice("");
    try {
      const reply = await readSettings(token, controller.signal, scopeRef.current);
      if (!controller.signal.aborted) receive(reply);
    } catch (cause) {
      if (!controller.signal.aborted) {
        const failure = settingsFailure(cause, false);
        setError(failure.message);
        if (failure.needsRefresh) setNeedsRefresh(true);
      }
    } finally {
      if (reading.current === controller) {
        reading.current = null;
        setLoading(false);
      }
    }
  }

  function openScope(next: SettingsScope) {
    if (blocked || !token || writing.current) return;
    if (open && scopeRef.current === next) return;
    reading.current?.abort();
    scopeRef.current = next;
    setScope(next);
    setSnapshot(undefined);
    setDraft(undefined);
    setErrors({});
    setError("");
    setNotice("");
    setNeedsRefresh(false);
    setOpen(true);
    void refresh();
  }

  function close() {
    if (writing.current) return;
    reading.current?.abort();
    reading.current = null;
    setLoading(false);
    setOpen(false);
  }

  function update<Key extends keyof SettingsDraft>(key: Key, value: SettingsDraft[Key]) {
    if (disabled || !snapshot) return;
    setDraft(
      (current) =>
        current &&
        (key === "agent" && typeof value === "string"
          ? selectProfile(current, value, snapshot.profiles)
          : { ...current, [key]: value }),
    );
    setErrors((current) => ({
      ...current,
      [key]: undefined,
      ...(key === "agent" ? { model: undefined } : {}),
    }));
    setNotice("");
  }

  async function write(reset = false) {
    if (disabled || writing.current || needsRefresh || !snapshot || !draft) return;
    const parsed = reset
      ? { ok: true as const, values: snapshot.defaults }
      : parseDraft(draft, snapshot.profiles);
    if (!parsed.ok) {
      setErrors(parsed.errors);
      setNotice("");
      return;
    }
    writing.current = true;
    setPending(true);
    setNotice("");
    setError("");
    try {
      const operated = await operate(async () => {
        try {
          const reply = reset
            ? await resetSettings(token, snapshot.revision, scopeRef.current)
            : await saveSettings(token, snapshot.revision, parsed.values, scopeRef.current);
          receive(reply);
          if (scopeRef.current === "global") accept(await readSettings(token, new AbortController().signal));
          setNotice(
            scopeRef.current === "global"
              ? "Global defaults saved. New workspaces inherit these defaults; saved workspace overrides take priority."
              : reset ? "Workspace overrides removed. Inherited defaults apply to your next run or recording."
              : "Workspace settings saved. Applies to your next run or recording.",
          );
        } catch (cause) {
          const failure = settingsFailure(cause, true);
          setError(failure.message);
          setNeedsRefresh(failure.needsRefresh);
        }
      });
      if (!operated) {
        setError(
          "The client is busy. Wait for the current operation to finish. Your draft is kept.",
        );
      }
    } finally {
      writing.current = false;
      setPending(false);
    }
  }

  return {
    token,
    open,
    snapshot,
    draft,
    errors,
    error,
    notice,
    needsRefresh,
    loading,
    pending,
    dirty,
    disabled,
    blocked,
    scope,
    show: () => openScope("workspace"),
    switchScope: (next: SettingsScope) => { if (open) openScope(next); },
    close,
    refresh,
    update,
    save: () => write(),
    reset: () => write(true),
  };
}
