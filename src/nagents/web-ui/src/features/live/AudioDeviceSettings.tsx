import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { canChooseAudioOutput, chooseAudioOutput, listAudioDevices, readAudioDevices, requestAudioDevices, saveAudioDevices, supportsAudioOutput } from "./devices.js";
import type { AudioDeviceList, AudioDeviceOption } from "./devices.js";
import type { AudioDeviceSelection, LiveTransport } from "./types.js";

export function AudioDeviceSettings({ transport = "websocket", disabled = false, active = false, activeSelection, applyDevice }: {
  transport?: LiveTransport; disabled?: boolean; active?: boolean;
  activeSelection?: AudioDeviceSelection;
  applyDevice?: (kind: "input" | "output", id: string) => Promise<void>;
}) {
  const [selection, setSelection] = useState(readAudioDevices);
  const [devices, setDevices] = useState<AudioDeviceList>({ inputs: [], outputs: [], needsPermission: false });
  const [loading, setLoading] = useState(true);
  const [requesting, setRequesting] = useState(false);
  const [applying, setApplying] = useState(false);
  const [error, setError] = useState("");
  const [storageNotice, setStorageNotice] = useState("");
  const mounted = useRef(false);
  const requestId = useRef(0);
  const permissionPending = useRef(false);
  const permissionAbort = useRef<AbortController | undefined>(undefined);
  const currentSelection = useRef(selection);
  const shownSelection = active && activeSelection ? activeSelection : selection;
  currentSelection.current = shownSelection;
  const applyingPending = useRef(false);
  const activeVoice = useRef(active);
  const restoreControl = useRef<HTMLElement | undefined>(undefined);
  activeVoice.current = active;
  const outputSupported = supportsAudioOutput(transport);
  const canRequest = typeof globalThis.navigator?.mediaDevices?.getUserMedia === "function";
  const busy = disabled || requesting || applying;

  function rememberControl(element: HTMLElement) {
    if (element.ownerDocument.activeElement === element) restoreControl.current = element;
  }
  useLayoutEffect(() => {
    if (busy || loading) return;
    const element = restoreControl.current;
    restoreControl.current = undefined;
    if (element?.isConnected && element.ownerDocument.activeElement === element.ownerDocument.body &&
      !element.closest("[hidden]") && !element.matches(":disabled") && element.checkVisibility?.() !== false)
      element.focus({ preventScroll: true });
  }, [busy, loading]);

  async function refresh(permission = false) {
    permission = permission && !activeVoice.current;
    const id = ++requestId.current;
    setLoading(true); setError("");
    if (permission) { permissionPending.current = true; permissionAbort.current = new AbortController(); setRequesting(true); }
    try {
      const next = await (permission ? requestAudioDevices(permissionAbort.current?.signal) : listAudioDevices());
      if (mounted.current && id === requestId.current) setDevices(next);
    } catch (cause) {
      if (mounted.current && id === requestId.current) setError(cause instanceof Error && cause.name === "NotAllowedError"
        ? "Microphone access was not allowed. Update your browser permissions, then try again."
        : "Could not list audio devices. Check your browser permissions, then try again.");
    } finally {
      if (permission) { permissionPending.current = false; if (mounted.current) setRequesting(false); }
      if (mounted.current && id === requestId.current) setLoading(false);
    }
  }

  async function select(key: "inputId" | "outputId", id: string): Promise<boolean> {
    if (disabled || applyingPending.current) return false;
    if (currentSelection.current[key] === id) return true;
    applyingPending.current = true;
    setApplying(true); setError("");
    try {
      await applyDevice?.(key === "inputId" ? "input" : "output", id);
      if (!mounted.current) return false;
      const next = { ...currentSelection.current, [key]: id };
      currentSelection.current = next;
      setSelection(next);
      setStorageNotice(saveAudioDevices({ ...readAudioDevices(), [key]: id }) ? "" : "Saved for this tab only. Your browser could not store this preference.");
      return true;
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error && cause.message
        ? cause.message
        : `Could not change the ${key === "inputId" ? "microphone" : "speaker"}. Your current choice is unchanged.`);
      return false;
    } finally {
      applyingPending.current = false;
      if (mounted.current) setApplying(false);
    }
  }

  async function chooseSpeaker() {
    ++requestId.current; permissionPending.current = true;
    setLoading(false);
    setRequesting(true); setError("");
    try {
      const device = await chooseAudioOutput();
      if (!mounted.current) return;
      if (await select("outputId", device.id)) setDevices(current => ({ ...current, outputs: [...current.outputs.filter(option => option.id !== device.id), device] }));
    } catch (cause) {
      if (mounted.current) setError(cause instanceof Error && cause.name === "NotAllowedError"
        ? "No speaker was selected. Your current choice is unchanged."
        : "Could not select that speaker. Try again or use System default.");
    } finally { permissionPending.current = false; if (mounted.current) setRequesting(false); }
  }

  useEffect(() => {
    mounted.current = true;
    void refresh();
    const changed = () => { if (!permissionPending.current) void refresh(); };
    const stored = () => {
      if (activeVoice.current || applyingPending.current) return;
      const next = readAudioDevices(); currentSelection.current = next; setSelection(next);
    };
    const media = globalThis.navigator?.mediaDevices;
    const focused = (event: FocusEvent) => {
      if (event.target !== restoreControl.current && event.target !== document.body) restoreControl.current = undefined;
    };
    document.addEventListener("focusin", focused);
    media?.addEventListener?.("devicechange", changed);
    globalThis.addEventListener?.("storage", stored);
    return () => {
      mounted.current = false; ++requestId.current;
      permissionAbort.current?.abort();
      restoreControl.current = undefined; document.removeEventListener("focusin", focused);
      media?.removeEventListener?.("devicechange", changed);
      globalThis.removeEventListener?.("storage", stored);
    };
  }, []);
  useEffect(() => {
    if (active) permissionAbort.current?.abort();
    else { const next = readAudioDevices(); currentSelection.current = next; setSelection(next); }
  }, [active]);

  function options(values: AudioDeviceOption[], selected: string, supported = true) {
    return <>
      <option value="">System default</option>
      {selected && !values.some(device => device.id === selected) && <option value={selected} disabled={!supported}>Saved device · unavailable</option>}
      {values.map(device => <option key={device.id} value={device.id} disabled={!supported}>{device.label}</option>)}
    </>;
  }

  return <section className="live-device-settings" aria-labelledby="live-devices-title" aria-busy={requesting || applying}>
    <header><h4 id="live-devices-title">Audio devices</h4><span>{active ? "Applies immediately" : "Saved in this browser"}</span></header>
    <p>{active ? "Ready to change during voice. Your choices are also saved in this browser." : "Microphone and speaker for your next conversation."}</p>
    <div className="live-device-pickers">
      <label><span><Icon name="mic" size={14} />Microphone</span><select aria-label="Microphone" value={shownSelection.inputId} disabled={busy} onChange={event => { rememberControl(event.currentTarget); void select("inputId", event.target.value); }}>{options(devices.inputs, shownSelection.inputId)}</select></label>
      <label><span><Icon name="volume" size={14} />Speaker</span><select aria-label="Speaker" value={shownSelection.outputId} disabled={busy || (!outputSupported && !shownSelection.outputId)} aria-describedby={!outputSupported ? "live-speaker-help" : undefined} onChange={event => { rememberControl(event.currentTarget); void select("outputId", event.target.value); }}>{options(devices.outputs, shownSelection.outputId, outputSupported)}</select></label>
    </div>
    {!outputSupported && <p id="live-speaker-help">{shownSelection.outputId ? "Speaker selection is unavailable in this browser. Choose System default to continue." : "This browser uses the system default speaker. Change it in your system audio settings."}</p>}
    {devices.needsPermission && <p>{active ? "Refresh devices to update available microphones and speakers." : "Allow microphone access to show device names. The microphone turns off immediately afterward."}</p>}
    {!canRequest && <p>Open a supported browser on localhost or HTTPS to choose audio devices.</p>}
    <div className="live-device-actions">
      <button type="button" disabled={busy || !canRequest || loading} onClick={event => { rememberControl(event.currentTarget); void refresh(devices.needsPermission && !active); }}>{requesting ? "Waiting for permission…" : devices.needsPermission && !active ? "Show devices" : "Refresh devices"}</button>
      {outputSupported && canChooseAudioOutput() && <button type="button" disabled={busy} onClick={event => { rememberControl(event.currentTarget); void chooseSpeaker(); }}>Choose speaker…</button>}
      {loading && !requesting && <span role="status">Checking devices…</span>}
      {applying && <span role="status">Changing audio device…</span>}
    </div>
    {error && <p className="live-device-feedback" role="alert">{error}</p>}
    {storageNotice && <p className="live-device-feedback" role="status">{storageNotice}</p>}
  </section>;
}
