import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { useChatMedia } from "./useChatMedia.js";

type MediaAsset = { filename: string; media_type: string; byte_length: number };
const safeName = (name: string) => name.replace(/[\u0000-\u001f\u007f-\u009f/\\]/g, "_");
const fileSize = (bytes: number) => bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.max(1, Math.ceil(bytes / 1024))} KB`;

function ImageViewer({ url, filename, close }: { url: string; filename: string; close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const title = useId();
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    element?.showModal();
    return () => {
      element?.close();
      if (previous instanceof window.HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, []);
  return <dialog ref={dialog} className="chat-image-viewer" aria-labelledby={title}
    onCancel={event => { event.preventDefault(); close(); }}
    onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <header>
      <h2 id={title}>{filename}</h2>
      <a className="chat-media-download" href={url} download={filename} aria-label={`Download ${filename}`}><Icon name="download" size={17} /><span>Download</span></a>
      <button type="button" className="chat-image-close" aria-label="Close image" onClick={close}><Icon name="close" size={21} /></button>
    </header>
    <div className="chat-image-stage"><img src={url} alt={filename} /></div>
  </dialog>;
}

export function ChatMedia({ token, path, asset, enabled = true, caption = "" }: {
  token: string; path: string; asset: MediaAsset; enabled?: boolean; caption?: string;
}) {
  const filename = safeName(asset.filename);
  const kind = asset.media_type.startsWith("image/") ? "image" : asset.media_type.startsWith("audio/") ? "audio" : "video";
  const [expanded, setExpanded] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [decodeError, setDecodeError] = useState("");
  const [ratio, setRatio] = useState(4 / 3);
  const [dimensions, setDimensions] = useState("");
  const media = useChatMedia({ token, path, asset, enabled, hold: expanded || playing });
  useEffect(() => { setExpanded(false); setPlaying(false); setDecodeError(""); setDimensions(""); setRatio(4 / 3); }, [token, path, asset.media_type, asset.byte_length]);
  useEffect(() => { setDecodeError(""); }, [media.url]);
  const error = decodeError || media.error;
  const loaded = !!media.url;
  const failed = () => { setDecodeError(`This ${kind} could not be displayed. You can still download the file.`); setPlaying(false); };
  const loading = media.status === "loading" || media.status === "waiting";
  return <figure ref={media.container} className={`chat-media chat-media-${kind}`} aria-label={filename} aria-busy={loading && enabled}>
    <div className={`chat-media-frame${!loaded && !error ? " is-loading" : ""}${error ? " has-error" : ""}`}
      style={kind === "image" ? { aspectRatio: ratio } : undefined}>
      {loaded && kind === "image" && !decodeError ? <button type="button" className="chat-image-open" aria-label={`Open image ${filename}`} onClick={() => setExpanded(true)}>
        <img src={media.url} alt={filename} decoding="async" onError={failed} onLoad={event => {
          const { naturalWidth, naturalHeight } = event.currentTarget;
          if (naturalWidth && naturalHeight) { setRatio(naturalWidth / naturalHeight); setDimensions(`${naturalWidth} × ${naturalHeight}`); }
        }} />
        <span className="chat-image-expand" aria-hidden="true"><Icon name="expand" size={18} /></span>
      </button> : loaded && kind === "audio" ? <audio src={media.url} controls preload="metadata" onError={failed}
        onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)} />
        : loaded && kind === "video" ? <video src={media.url} controls preload="metadata" onError={failed}
          onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)} /> : null}
      {error ? <div className="chat-media-feedback" role="status">
        <Icon name={kind === "image" ? "image" : "paperclip"} size={25} />
        <span>{decodeError || `Couldn’t load this ${kind}.`}</span>
        {!decodeError && <button type="button" onClick={media.retry}>Retry</button>}
      </div> : !loaded ? <div className="chat-media-placeholder" role="status">
        <Icon name={kind === "image" ? "image" : "paperclip"} size={28} />
        <span className="sr-only">{enabled ? `Loading ${kind}…` : `${kind === "image" ? "Image" : "Media"} will be available when saved.`}</span>
      </div> : null}
    </div>
    <figcaption className="chat-media-caption">
      <div className="chat-media-file"><span className="chat-media-name" title={filename}>{filename}</span>
        <span className="chat-media-meta">{dimensions ? `${dimensions} · ` : ""}{fileSize(asset.byte_length)}</span>
        {caption && <span className="chat-media-description">{caption}</span>}</div>
      {loaded ? <a className="chat-media-download" href={media.url} download={filename} aria-label={`Download ${filename}`}><Icon name="download" size={16} /><span>Download</span></a>
        : <span className="chat-media-download is-pending" aria-hidden="true"><Icon name="download" size={16} /><span>Download</span></span>}
    </figcaption>
    {expanded && media.url && <ImageViewer url={media.url} filename={filename} close={() => setExpanded(false)} />}
  </figure>;
}
