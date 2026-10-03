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
  return <section className="voice-context-details inspection-compact" aria-labelledby="voice-context-title">
    <header><h3 id="voice-context-title" tabIndex={-1}>Voice context</h3><button type="button" onClick={close} aria-label="Collapse voice context"><Icon name="close" size={16} /></button></header>
    <p>{context.notice}</p>
    {!!context.message_count && <dl><dt>Starting context</dt><dd>{context.method === "generated_summary" ? "Prepared brief and recent conversation" : context.summary_included ? "Saved summary and recent conversation" : "Recent conversation"}</dd><dt>Included</dt><dd>{context.message_count} text items · {(context.bytes / 1000).toFixed(1)} KB of a 7 KB limit</dd></dl>}
    {context.omitted_content && <p>Some history or non-text content was left out. Ask your assistant for an earlier detail or a fuller recap when needed.</p>}
    {loading && <p role="status">Loading captured voice context…</p>}
    {error && <p className="delegation-inspector-error" role="alert">{error}</p>}
    {current && !current.available && <p className="inspection-source-note">{current.reason || "Starting context was not captured for this voice session."}</p>}
    {current?.available && <>
      <InspectionPayload title="Voice instructions" value={current.instructions} empty="Voice instructions were not captured." note="Instructions prepared for this voice session." />
      {current.history_truncated && <p className="delegation-truncation">The retained startup history is incomplete. This is a preview of the captured seed.</p>}
      <InspectionPayload title="Voice startup history" value={{ text: history, characters: Array.from(history).length, truncated: false }} empty="Startup history was not captured." note={current.history.length ? "Messages prepared for this voice session at startup. The JSON formatting below is for inspection." : current.history_truncated ? "No messages fit in the retained preview. Startup history was omitted from this capture." : "No history messages were included in the prepared startup payload."} />
    </>}
    {context.chat_session_id && <p className="voice-context-footnote">Your main assistant uses this chat’s saved history and summaries for requests that need more context. Change what voice starts with in Voice settings → Starting context.</p>}
    <footer><button type="button" disabled={loading} onClick={() => refresh(value => value + 1)}>Refresh context</button></footer>
  </section>;
}
