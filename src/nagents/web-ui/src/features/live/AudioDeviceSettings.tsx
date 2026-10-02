import { useEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { canChooseAudioOutput, chooseAudioOutput, listAudioDevices, readAudioDevices, requestAudioDevices, saveAudioDevices, supportsAudioOutput } from "./devices.js";
import type { AudioDeviceList, AudioDeviceOption } from "./devices.js";
import type { LiveTransport } from "./types.js";

export function AudioDeviceSettings({ transport = "websocket", disabled = false }: { transport?: LiveTransport; disabled?: boolean }) {
  const [selection, setSelection] = useState(readAudioDevices);
  const [devices, setDevices] = useState<AudioDeviceList>({ inputs: [], outputs: [], needsPermission: false });
  const [loading, setLoading] = useState(true);
  const [requesting, setRequesting] = useState(false);
  const [error, setError] = useState("");
  const [storageNotice, setStorageNotice] = useState("");
  const mounted = useRef(false);
  const requestId = useRef(0);
  const permissionPending = useRef(false);
  const permissionAbort = useRef<AbortController | undefined>(undefined);
  const currentSelection = useRef(selection);
  const outputSupported = supportsAudioOutput(transport);
  const canRequest = typeof globalThis.navigator?.mediaDevices?.getUserMedia === "function";
  const busy = disabled || requesting;

  async function refresh(permission = false) {
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

  function select(key: "inputId" | "outputId", id: string) {
    const next = { ...currentSelection.current, [key]: id };
    currentSelection.current = next;
    setSelection(next);
    setStorageNotice(saveAudioDevices(next) ? "" : "Saved for this tab only. Your browser could not store this preference.");
  }

  async function chooseSpeaker() {
    ++requestId.current; permissionPending.current = true;
    setLoading(false);
    setRequesting(true); setError("");
    try {
      const device = await chooseAudioOutput();
      if (!mounted.current) return;
      select("outputId", device.id);
      setDevices(current => ({ ...current, outputs: [...current.outputs.filter(option => option.id !== device.id), device] }));
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
    const stored = () => { const next = readAudioDevices(); currentSelection.current = next; setSelection(next); };
    const media = globalThis.navigator?.mediaDevices;
    media?.addEventListener?.("devicechange", changed);
    globalThis.addEventListener?.("storage", stored);
    return () => {
      mounted.current = false; ++requestId.current;
      permissionAbort.current?.abort();
      media?.removeEventListener?.("devicechange", changed);
      globalThis.removeEventListener?.("storage", stored);
    };
  }, []);

  function options(values: AudioDeviceOption[], selected: string, supported = true) {
    return <>
      <option value="">System default</option>
      {selected && !values.some(device => device.id === selected) && <option value={selected} disabled={!supported}>Saved device · unavailable</option>}
      {values.map(device => <option key={device.id} value={device.id} disabled={!supported}>{device.label}</option>)}
    </>;
  }

  return <section className="live-device-settings" aria-labelledby="live-devices-title" aria-busy={requesting}>
    <header><h4 id="live-devices-title">On this device</h4><span>THIS BROWSER</span></header>
    <p>Saved automatically in this browser. Changes apply to your next conversation.</p>
    <div className="live-device-pickers">
      <label><span><Icon name="mic" size={14} />Microphone</span><select aria-label="Microphone" value={selection.inputId} disabled={busy} onChange={event => select("inputId", event.target.value)}>{options(devices.inputs, selection.inputId)}</select></label>
      <label><span><Icon name="volume" size={14} />Speaker</span><select aria-label="Speaker" value={selection.outputId} disabled={busy || (!outputSupported && !selection.outputId)} aria-describedby={!outputSupported ? "live-speaker-help" : undefined} onChange={event => select("outputId", event.target.value)}>{options(devices.outputs, selection.outputId, outputSupported)}</select></label>
    </div>
    {!outputSupported && <p id="live-speaker-help">{selection.outputId ? "Speaker selection is unavailable for this connection in your browser. Choose System default to continue." : "Your browser uses the system default speaker for this connection. Change it in your system audio settings."}</p>}
    {devices.needsPermission && <p>Allow microphone access to show device names. The microphone turns off immediately afterward.</p>}
    {!canRequest && <p>Open a supported browser on localhost or HTTPS to choose audio devices.</p>}
    <div className="live-device-actions">
      <button type="button" disabled={busy || !canRequest || loading} onClick={() => void refresh(devices.needsPermission)}>{requesting ? "Waiting for permission…" : devices.needsPermission ? "Show devices" : "Refresh devices"}</button>
      {outputSupported && canChooseAudioOutput() && <button type="button" disabled={busy} onClick={() => void chooseSpeaker()}>Choose speaker…</button>}
      {loading && !requesting && <span role="status">Checking devices…</span>}
    </div>
    {error && <p className="live-device-feedback" role="alert">{error}</p>}
    {storageNotice && <p className="live-device-feedback" role="status">{storageNotice}</p>}
  </section>;
}
