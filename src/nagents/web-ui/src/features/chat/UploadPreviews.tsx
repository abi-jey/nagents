import type { UploadState } from "./uploads.js";

export function UploadPreviews({ state, remove, disabled }: { state: UploadState; remove: (id: string) => void; disabled: boolean }) {
  return <div className="upload-drafts" aria-label="Draft attachments">
    {state.error && <p role="status">{state.error}</p>}
    {state.items.map((item) => <section key={item.id} className="upload-draft">
      {item.url && <img src={item.url} alt={`Draft: ${item.name}`} />}
      <span>{item.name} · {item.type}</span>
      <span role="status">{item.status === "uploading" ? "Uploading draft…" : item.status === "ready" ? "Ready to send" : item.error}</span>
      <button type="button" disabled={disabled} onClick={() => remove(item.id)} aria-label={`Remove ${item.name}`}>Remove</button>
    </section>)}
  </div>;
}
