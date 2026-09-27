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
import type { SettingsReply, SettingsValues } from "./types.js";
import type { SettingsScope } from "./transport.js";

const refreshInterval = 4000;

function changed(snapshot: SettingsReply, draft: SettingsDraft) {
  return Object.entries(createDraft(snapshot.values)).some(
    ([key, value]) => draft[key as keyof SettingsDraft] !== value,
  );
}

function savedValuesChanged(previous: SettingsReply, next: SettingsReply) {
  return previous.revision !== next.revision || Object.entries(next.values).some(
    ([key, value]) => previous.values[key as keyof SettingsValues] !== value,
  );
}

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
  const [readError, setReadError] = useState("");
  const [notice, setNotice] = useState("");
  const [needsRefresh, setNeedsRefresh] = useState(false);
  const [changedElsewhere, setChangedElsewhere] = useState(false);
  const [loading, setLoading] = useState(false);
  const [pending, setPending] = useState(false);
  const reading = useRef<AbortController | null>(null);
  const writing = useRef(false);
  const openRef = useRef(false);
  const snapshotRef = useRef<SettingsReply>(undefined);
  const draftRef = useRef<SettingsDraft>(undefined);
  const recoveryRef = useRef(false);
  const poll = useRef<() => void>(() => {});

  useEffect(() => () => {
    openRef.current = false;
    reading.current?.abort();
  }, []);

  useEffect(() => {
    if (!open) return;
    const timer = setInterval(() => poll.current(), refreshInterval);
    return () => clearInterval(timer);
  }, [open]);

  const dirty = !!snapshot && !!draft && changed(snapshot, draft);
  const disabled = blocked || loading || pending;

  function receive(reply: SettingsReply) {
    snapshotRef.current = reply;
    draftRef.current = createDraft(reply.values);
    setSnapshot(reply);
    setDraft(draftRef.current);
    setErrors({});
    setError("");
    setReadError("");
    recoveryRef.current = false;
    setNeedsRefresh(false);
    setChangedElsewhere(false);
    if (scopeRef.current === "workspace") accept(reply);
  }

  async function load(mode: "initial" | "poll" | "applied" | "reload") {
    if (!openRef.current || writing.current || blocked || (mode !== "reload" && recoveryRef.current)) return;
    if (mode === "poll" && reading.current) return;
    reading.current?.abort();
    const controller = new AbortController();
    reading.current = controller;
    if (mode === "initial" || mode === "reload") setLoading(true);
    try {
      const reply = await readSettings(token, controller.signal, scopeRef.current);
      if (controller.signal.aborted || !openRef.current || reading.current !== controller) return;
      setReadError("");
      const previous = snapshotRef.current;
      const currentDraft = draftRef.current;
      if (mode === "reload" || mode === "initial" || !previous || !currentDraft) {
        receive(reply);
      } else if (changed(previous, currentDraft) && savedValuesChanged(previous, reply)) {
        // A background read must not silently turn a stale draft into a write against new saved values.
        recoveryRef.current = true;
        setNeedsRefresh(true);
        setChangedElsewhere(true);
      } else if (JSON.stringify(reply) !== JSON.stringify(previous)) {
        snapshotRef.current = reply;
        setSnapshot(reply);
        if (!changed(previous, currentDraft)) {
          draftRef.current = createDraft(reply.values);
          setDraft(draftRef.current);
        }
        if (scopeRef.current === "workspace") accept(reply);
        setNotice("");
      }
    } catch (cause) {
      if (!controller.signal.aborted && openRef.current && reading.current === controller) {
        const failure = settingsFailure(cause, false);
        if (mode === "initial") setError(failure.message);
        else if (snapshotRef.current) setReadError(failure.message);
      }
    } finally {
      if (reading.current === controller) {
        reading.current = null;
        setLoading(false);
      }
    }
  }

  poll.current = () => { void load("poll"); };

  function openScope(next: SettingsScope) {
    if (blocked || !token || writing.current) return;
    reading.current?.abort();
    reading.current = null;
    openRef.current = true;
    scopeRef.current = next;
    setScope(next);
    snapshotRef.current = undefined;
    draftRef.current = undefined;
    setSnapshot(undefined);
    setDraft(undefined);
    setErrors({});
    setError("");
    setReadError("");
    setNotice("");
    recoveryRef.current = false;
    setNeedsRefresh(false);
    setChangedElsewhere(false);
    setOpen(true);
    void load("initial");
  }

  function close() {
    if (writing.current) return;
    openRef.current = false;
    reading.current?.abort();
    reading.current = null;
    setLoading(false);
    setOpen(false);
  }

  function update<Key extends keyof SettingsDraft>(key: Key, value: SettingsDraft[Key]) {
    const current = draftRef.current;
    const base = snapshotRef.current;
    if (disabled || !base || !current) return;
    draftRef.current = key === "agent" && typeof value === "string"
      ? selectProfile(current, value, base.profiles)
      : { ...current, [key]: value };
    setDraft(draftRef.current);
    setErrors((current) => ({
      ...current,
      [key]: undefined,
      ...(key === "agent" ? { model: undefined } : {}),
    }));
    setNotice("");
  }

  async function write(reset = false) {
    const currentSnapshot = snapshotRef.current;
    const currentDraft = draftRef.current;
    if (disabled || writing.current || recoveryRef.current || !currentSnapshot || !currentDraft) return;
    const parsed = reset
      ? { ok: true as const, values: currentSnapshot.defaults }
      : parseDraft(currentDraft, currentSnapshot.profiles);
    if (!parsed.ok) {
      setErrors(parsed.errors);
      setNotice("");
      return;
    }
    writing.current = true;
    reading.current?.abort();
    reading.current = null;
    setPending(true);
    setNotice("");
    setError("");
    setReadError("");
    try {
      const operated = await operate(async () => {
        try {
          const reply = reset
            ? await resetSettings(token, currentSnapshot.revision, scopeRef.current)
            : await saveSettings(token, currentSnapshot.revision, parsed.values, scopeRef.current);
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
          recoveryRef.current = failure.needsRefresh;
          setNeedsRefresh(failure.needsRefresh);
          setChangedElsewhere(false);
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
    readError,
    notice,
    needsRefresh,
    changedElsewhere,
    loading,
    pending,
    dirty,
    disabled,
    blocked,
    scope,
    show: () => openScope("workspace"),
    showGlobal: () => openScope("global"),
    close,
    refresh: () => load("applied"),
    reload: () => load("reload"),
    update,
    save: () => write(),
    reset: () => write(true),
  };
}
