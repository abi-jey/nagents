import { createContext, useContext, useEffect, useRef, useState } from "react";
import { loadMedia } from "./deliveries.js";
import { MediaToken } from "./LocalDelivery.js";
import type { UploadMetadata } from "./uploads.js";

export const UploadSession = createContext("");

export function UploadAttachment({ upload, ready }: { upload: UploadMetadata; ready: boolean }) {
  const token = useContext(MediaToken);
  const session = useContext(UploadSession);
  const container = useRef<HTMLDivElement>(null);
  const owned = useRef<(() => void) | undefined>(undefined);
  const [visible, setVisible] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const image = upload.media_type.startsWith("image/");

  useEffect(() => {
    if (!image || !ready || !container.current || visible) return;
    if (typeof IntersectionObserver === "undefined") { setVisible(true); return; }
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) setVisible(true);
    }, { rootMargin: "200px" });
    observer.observe(container.current);
    return () => observer.disconnect();
  }, [image, ready, visible]);

  useEffect(() => {
    if (!image || !token || !session || !ready || !visible) return;
    const abort = new AbortController();
    setUrl(""); setError("");
    const path = ["sessions", session, "uploads", upload.upload_id, "preview"].map(encodeURIComponent).join("/");
    void loadMedia(token, path, upload, abort.signal).then((loaded) => {
      if (abort.signal.aborted) { loaded.release(); return; }
      owned.current = loaded.release; setUrl(loaded.url);
    }).catch((cause) => {
      if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : "Image preview is unavailable.");
    });
    return () => { abort.abort(); owned.current?.(); owned.current = undefined; };
  }, [image, token, session, ready, visible, attempt, upload.upload_id, upload.media_type, upload.byte_length]);

  return <div className="uploaded-attachment" ref={container}>
    <p className="record-note">Attachment: {upload.filename} · {upload.media_type} · {Math.ceil(upload.byte_length / 1024)} KiB</p>
    {image && ready && (url && !error ? <img className="channel-image" src={url} alt={`Attachment: ${upload.filename}`} onError={() => {
      owned.current?.(); owned.current = undefined; setUrl(""); setError("Image preview is unavailable.");
    }} /> : error ? <><span className="record-note" role="status">{error}</span><button type="button" onClick={() => setAttempt((current) => current + 1)}>Retry preview</button></>
      : visible ? <span className="record-note" role="status">Loading image preview…</span>
        : <button type="button" onClick={() => setVisible(true)}>Load image preview</button>)}
  </div>;
}
