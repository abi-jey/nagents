import { useEffect, useRef, useState } from "react";

/** A missing explicitly chosen speaker needs user consent before routing elsewhere. */
export function OutputDeviceNotice({ connected, outputId, hidden, useDefault }: {
  connected: boolean; outputId: string; hidden: boolean; useDefault(): Promise<void>;
}) {
  const [missing, setMissing] = useState("");
  const [changing, setChanging] = useState(false);
  const [error, setError] = useState("");
  const generation = useRef(0);
  useEffect(() => {
    const epoch = ++generation.current;
    setMissing(""); setError(""); setChanging(false);
    const media = globalThis.navigator?.mediaDevices;
    if (!connected || !outputId || !media?.enumerateDevices) return;
    let seen = false, request = 0;
    const refresh = async () => {
      const revision = ++request;
      try {
        const devices = await media.enumerateDevices();
        if (generation.current !== epoch || revision !== request) return;
        if (devices.some(device => device.kind === "audiooutput" && device.deviceId === outputId)) {
          seen = true; setMissing(""); setError("");
        } else if (seen) setMissing(outputId);
      } catch { /* Enumeration failure or filtered initial inventory proves no unplug. */ }
    };
    const changed = () => { void refresh(); };
    media.addEventListener?.("devicechange", changed);
    void refresh();
    return () => { generation.current++; media.removeEventListener?.("devicechange", changed); };
  }, [connected, outputId]);

  async function recover() {
    const epoch = generation.current;
    setChanging(true); setError("");
    try { await useDefault(); }
    catch (cause) {
      if (generation.current === epoch) setError(cause instanceof Error ? cause.message : "Could not change the speaker. Choose another speaker in Audio.");
    } finally { if (generation.current === epoch) setChanging(false); }
  }
  if (!connected || !outputId || missing !== outputId || hidden) return null;
  return <div className="voice-feedback voice-device-notice" role="status">
    <p>Your selected speaker is no longer available. Choose another in Audio, or use the system default for this session.</p>
    {error && <p role="alert">{error}</p>}
    <button type="button" disabled={changing} onClick={() => void recover()}>{changing ? "Changing speaker…" : "Use system default"}</button>
  </div>;
}
