import { createContext, useContext } from "react";
import { ChatMedia } from "./ChatMedia.js";
import { MediaToken } from "./LocalDelivery.js";
import type { UploadMetadata } from "./uploads.js";

export const UploadSession = createContext("");

export function UploadAttachment({ upload, ready }: { upload: UploadMetadata; ready: boolean }) {
  const token = useContext(MediaToken);
  const session = useContext(UploadSession);
  const path = ["sessions", session, "uploads", upload.upload_id, "preview"].map(encodeURIComponent).join("/");
  return upload.media_type.startsWith("image/")
    ? <ChatMedia key={JSON.stringify([path, upload.media_type, upload.byte_length])} token={token} path={path} asset={upload} enabled={ready && !!session} />
    : <p className="record-note">{upload.filename} · {Math.ceil(upload.byte_length / 1024)} KB</p>;
}
