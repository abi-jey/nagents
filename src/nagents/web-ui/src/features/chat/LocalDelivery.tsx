import { createContext, useContext, useEffect, useRef, useState } from "react";
import { loadAsset, mediaBudget, MEDIA_TYPES, type DeliveryAsset, type LocalDelivery } from "./deliveries.js";
import { MessageContent } from "./MessageContent.js";

export const MediaToken = createContext("");
const safeName = (name: string) => name.replace(/[\u0000-\u001f\u007f-\u009f/\\]/g, "_");

function Asset({ delivery, asset }: { delivery: LocalDelivery; asset: DeliveryAsset }) {
  const token = useContext(MediaToken);
  const [url, setUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const container = useRef<HTMLElement>(null);
  const attempted = useRef(false);
  const image = asset.media_type.startsWith("image/") && MEDIA_TYPES.has(asset.media_type);
  const owned = useRef<{ abort: AbortController; release?: () => void } | undefined>(undefined);
  function unload() {
    attempted.current = true;
    owned.current?.abort.abort(); owned.current?.release?.(); owned.current = undefined;
    setUrl(""); setLoading(false); setError("");
  }
  useEffect(() => {
    unload(); attempted.current = false;
    let observer: IntersectionObserver | undefined;
    let active = true;
    if (image && token && container.current) {
      const start = () => {
        if (!active || attempted.current) return;
        attempted.current = true; observer?.disconnect(); void load();
      };
      if (typeof IntersectionObserver === "undefined") start();
      else {
        observer = new IntersectionObserver(entries => {
          if (entries.some(entry => entry.isIntersecting)) start();
        }, { rootMargin: "200px" });
        observer.observe(container.current);
      }
    }
    return () => {
      active = false; attempted.current = true; observer?.disconnect();
      owned.current?.abort.abort(); owned.current?.release?.(); owned.current = undefined;
    };
  }, [token, delivery.session_id, delivery.delivery_id, asset.asset_id, asset.media_type, asset.byte_length]);
  async function load() {
    unload();
    const current = { abort: new AbortController(), release: undefined as (() => void) | undefined };
    owned.current = current; setLoading(true);
    try {
      const loaded = await loadAsset(token, delivery, asset, current.abort.signal, mediaBudget, image);
      if (owned.current !== current || current.abort.signal.aborted) { loaded.release(); return; }
      current.release = loaded.release; setUrl(loaded.url);
    } catch (error) {
      if (owned.current === current && !current.abort.signal.aborted)
        setError(error instanceof Error ? error.message : "Media could not be loaded.");
    } finally { if (owned.current === current) setLoading(false); }
  }
  const failed = () => setError("This browser could not decode the file. Use Download to open it elsewhere.");
  return <section ref={container} className="delivery-asset" aria-label={safeName(asset.filename)}>
    <p>{safeName(asset.filename)} <span className="record-note">{asset.media_type} · {Math.ceil(asset.byte_length / 1024)} KiB</span></p>
    {url && (image ? <img key={url} src={url} alt={safeName(asset.filename)} decoding="async" onError={failed} />
      : asset.media_type.startsWith("audio/") ? <audio src={url} controls preload="metadata" onError={failed} />
        : <video src={url} controls preload="metadata" onError={failed} />)}
    {error && <p role="status">{error}</p>}
    {loading ? <><div className={image ? "delivery-image-loading" : undefined} role="status">{image ? "Loading image preview…" : "Loading media…"}</div><button type="button" onClick={unload}>Cancel loading</button></>
      : url ? <><a href={url} download={safeName(asset.filename)}>Download</a><button type="button" onClick={unload}>Unload preview</button></>
        : MEDIA_TYPES.has(asset.media_type) ? <button type="button" onClick={() => void load()}>{image ? error ? "Retry preview" : "Show image preview" : "Load preview / download"}</button>
          : <p>Attachment preview unavailable for this type.</p>}
  </section>;
}

export function LocalDeliveryCard({ delivery }: { delivery: LocalDelivery }) {
  return <section className="local-delivery" aria-label="Explicit delivery" data-delivery-id={delivery.delivery_id}>
    <div className="entry-label">Explicit delivery · {delivery.channel}</div>
    {delivery.earlier && <p className="record-note">Delivery from earlier context</p>}
    <MessageContent text={delivery.text} />
    {delivery.assets.map((asset) => <Asset key={JSON.stringify([delivery.session_id, delivery.delivery_id, asset.asset_id])} delivery={delivery} asset={asset} />)}
  </section>;
}
