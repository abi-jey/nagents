import { useEffect, useState, useSyncExternalStore } from "react";
import { UploadDraft } from "./uploads.js";

export function useUploads(token: string, sessionId: string, configuration: string) {
  const [uploads] = useState(() => new UploadDraft());
  const uploadState = useSyncExternalStore(uploads.subscribe, uploads.getSnapshot, uploads.getSnapshot);
  useEffect(() => { uploads.configure(token, sessionId); }, [uploads, token, sessionId, configuration]);
  useEffect(() => () => uploads.dispose(), [uploads]);
  return { uploads, uploadState };
}
