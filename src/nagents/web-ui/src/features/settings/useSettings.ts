import { useEffect, useRef, useState } from "react";
import {
  createDraft,
  parseDraft,
  selectProfile,
  type DraftErrors,
  type SettingsDraft,
} from "./draft";
import {
  readSettings,
  resetSettings,
  saveSettings,
  settingsFailure,
} from "./transport";
import type { SettingsReply } from "./types";

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
    !!snapshot &&
    !!draft &&
    Object.entries(createDraft(snapshot.values)).some(
      ([key, value]) => draft[key as keyof SettingsDraft] !== value,
    );
  const disabled = blocked || loading || pending;

  function receive(reply: SettingsReply) {
    setSnapshot(reply);
    setDraft(createDraft(reply.values));
    setErrors({});
    setError("");
    setNeedsRefresh(false);
    accept(reply);
  }

  async function refresh() {
    if (writing.current || blocked) return;
    reading.current?.abort();
    const controller = new AbortController();
    reading.current = controller;
    setLoading(true);
    setNotice("");
    try {
      const reply = await readSettings(token, controller.signal);
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

  function show() {
    if (blocked || !token || writing.current) return;
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
            ? await resetSettings(token, snapshot.revision)
            : await saveSettings(token, snapshot.revision, parsed.values);
          receive(reply);
          setNotice(
            reset
              ? "Startup defaults restored. Saved overrides removed. Applies to your next run or recording."
              : "Settings saved. Applies to your next run or recording.",
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
    show,
    close,
    refresh,
    update,
    save: () => write(),
    reset: () => write(true),
  };
}
