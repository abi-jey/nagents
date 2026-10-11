import { useEffect, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { voiceContextDetails } from "./api.js";
import { InspectionPayload } from "./InspectionPayload.js";
import type { VoiceContext, VoiceContextDetailsRecord } from "./types.js";

export function VoiceContextDetails({ context, token, sessionId, close }: { context: VoiceContext; token: string; sessionId: string; close(): void }) {
  const [details, setDetails] = useState<VoiceContextDetailsRecord>();
  const [detailsToken, setDetailsToken] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, refresh] = useState(0);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setError("");
    void voiceContextDetails(token, sessionId, context, AbortSignal.any([abort.signal, AbortSignal.timeout(12_000)]))
      .then(value => { if (!abort.signal.aborted) { setDetails(value); setDetailsToken(token); } })
      .catch((cause: unknown) => { if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not load voice context."); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [token, sessionId, context.chat_session_id, context.fingerprint, revision]);
  const current = detailsToken === token && details?.voice_session_id === sessionId && details.chat_session_id === context.chat_session_id && details.fingerprint === context.fingerprint ? details : undefined;
  const history = current ? JSON.stringify(current.history, null, 2) : "";
  const source = !context.message_count ? "No saved context"
    : context.method === "generated_summary" ? "Chat brief + recent messages"
    : context.summary_included ? "Saved summary + recent messages" : "Recent chat";
  return <section className="voice-context-details inspection-compact" aria-labelledby="voice-context-title">
    <header><h3 id="voice-context-title" tabIndex={-1}>Voice context</h3><button type="button" onClick={close} aria-label="Collapse voice context"><Icon name="close" size={16} /></button></header>
    <p className="voice-context-summary">{source}{context.method === "recent_fallback" ? " · Summary unavailable" : ""}{context.omitted_content ? " · Partial history" : ""}</p>
    {loading && <p role="status">Loading captured voice context…</p>}
    {error && <p className="delegation-inspector-error" role="alert">{error}</p>}
    {current && !current.available && <p className="inspection-source-note">{current.reason || "Starting context was not captured for this voice session."}</p>}
    {current?.available && <>
      <InspectionPayload title="Voice instructions" value={current.instructions} empty="Voice instructions were not captured." note="Instructions prepared for this voice session." />
      {current.history_truncated && <p className="delegation-truncation">The retained startup history is incomplete. This is a preview of the captured seed.</p>}
      <InspectionPayload title="Voice startup history" value={{ text: history, characters: Array.from(history).length, truncated: false }} empty="Startup history was not captured." note={current.history.length ? "Messages prepared for this voice session at startup. The JSON formatting below is for inspection." : current.history_truncated ? "No messages fit in the retained preview. Startup history was omitted from this capture." : "No history messages were included in the prepared startup payload."} />
    </>}
    <details className="delegation-payload inspection-payload">
      <summary><span className="inspection-payload-title">Seed details</span><span>{context.message_count} text items · {(context.bytes / 1000).toFixed(1)} KB</span></summary>
      {context.notice && <p>{context.notice}</p>}
      <p>ngn includes up to 7,000 UTF-8 bytes of starting chat text. This is a startup budget, not the model’s context window.</p>
      {context.omitted_content && <p>Older text or non-text content was omitted. The main assistant retains the saved conversation.</p>}
      {context.chat_session_id && <p className="inspection-source-note">Configure this in Voice settings → Starting context.</p>}
    </details>
    <footer><button type="button" disabled={loading} onClick={() => refresh(value => value + 1)}>Refresh context</button></footer>
  </section>;
}
