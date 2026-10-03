import { useEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { delegationDetails } from "./api.js";
import { InspectionPayload, InspectionText } from "./InspectionPayload.js";
import type { LiveDelegation, LiveDelegationDetails, LiveModelRequest } from "./types.js";

const captureNote = (item: LiveModelRequest) => item.type === "model_context"
  ? "Post-plugin model input, before provider encoding."
  : "Observed provider request body chunk. Multiple chunks may belong to the same attempt.";

function CaptureIdentity({ item }: { item: LiveModelRequest }) {
  return <dl className="inspection-identifiers"><dt>Model call</dt><dd>{item.model_call_id}</dd><dt>Round</dt><dd>{item.round}</dd>{item.attempt_id && <><dt>Attempt</dt><dd>{item.attempt_id}</dd></>}</dl>;
}

export function DelegationInspector({ token, delegation, delegations, select, close, viewChat }: {
  token: string; delegation: LiveDelegation; delegations: LiveDelegation[];
  select(id: string): void; close(): void; viewChat(runId: string): void;
}) {
  const [details, setDetails] = useState<LiveDelegationDetails>();
  const [detailsToken, setDetailsToken] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, refresh] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); }, []);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setError("");
    void delegationDetails(token, delegation, AbortSignal.any([abort.signal, AbortSignal.timeout(12_000)]))
      .then(value => { if (!abort.signal.aborted) { setDetails(value); setDetailsToken(token); } })
      .catch((cause: unknown) => { if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not load delegation details."); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [token, delegation.id, delegation.sessionId, delegation.chatSessionId, delegation.runId, delegation.seq, revision]);
  const current = detailsToken === token && details?.delegation_id === delegation.id && details.voice_session_id === delegation.sessionId && details.chat_session_id === delegation.chatSessionId && (!delegation.runId || details.run_id === delegation.runId) ? details : undefined;
  const earlierCaptures = current?.model_requests?.filter(item => !current.timeline.some(event => event.seq === item.seq)) || [];
  return <section className="delegation-inspector inspection-compact" aria-labelledby="delegation-inspector-title">
    <header><div><Icon name="channels" size={17} /><h3 id="delegation-inspector-title" ref={heading} tabIndex={-1}>Delegation details</h3></div><button type="button" aria-label="Close delegation details" onClick={close}><Icon name="close" /></button></header>
    <div className="delegation-inspector-body">
      <p className="delegation-inspector-intro">Captured request data and processing events. Expand a row to inspect its payload.</p>
      {delegations.length > 1 && <label className="delegation-request-picker">Request<select value={delegation.id} onChange={event => select(event.target.value)}>{delegations.map((item, index) => <option key={item.id} value={item.id}>Request {index + 1} · {item.agent} · {item.status}</option>)}</select></label>}
      <div className="delegation-target"><strong>{current?.agent || delegation.agent}</strong><span>{current?.provider || delegation.provider} · {current?.model || delegation.model}</span></div>
      {loading && <p className="delegation-inspector-notice" role="status">Updating request details…</p>}
      {error && <p className="delegation-inspector-error" role="alert">{error}</p>}
      {current && <>
        <h4>Processing</h4>
        {current.timeline_truncated && <p className="delegation-truncation">Showing the most recent events.</p>}
        <ol className="delegation-timeline">{current.timeline.map(event => {
          const capture = current.model_requests?.find(item => item.seq === event.seq);
          const isModelEvent = event.type === "model_context" || event.type === "http_request_body";
          return <li key={event.seq} data-status={event.status} data-event-type={event.type || "delegation"}>
          <details className="inspection-event"><summary>
            <code className="inspection-event-type">{event.type || "delegation"}</code><span className="delegation-event-status">{event.status}</span><small>#{event.seq}</small><span className="inspection-chevron" aria-hidden="true">›</span>
            <span className="inspection-event-text">{event.capture_limited ? "Capture retention limit reached" : event.text}</span>
          </summary>
            {capture && <><p className="inspection-source-note">{captureNote(capture)}</p><InspectionText value={capture.payload} /><CaptureIdentity item={capture} /></>}
            {isModelEvent && !capture && <p className="inspection-source-note">{event.capture_limited ? "This event marks a capture limit. No additional request payload was retained." : "This request payload is no longer retained."}</p>}
            {isModelEvent ? <details className="inspection-event-metadata"><summary>Event metadata</summary><pre tabIndex={0}>{JSON.stringify(event, null, 2)}</pre></details> : <pre tabIndex={0}>{JSON.stringify(event, null, 2)}</pre>}
          </details>
        </li>; })}</ol>
        <p className="delegation-source-note">Application lifecycle events and capture metadata. Tool execution and approvals are shown in the chat.</p>
        <InspectionPayload title="Speech transcript context" value={current.request.transcript} empty="No speech transcript was captured." note="Partial speech retained when this request was received. Captions can arrive later." />
        <InspectionPayload title="Assistant request input" value={current.request.input} empty="No assistant request input was captured." note="The request prepared for the assistant. Captured model inputs and HTTP body chunks are listed below." />
        {current.model_requests_truncated && <p className="delegation-truncation">Some model request captures were omitted because the retention limit was reached.</p>}
        {!!earlierCaptures.length && <h4>Earlier model captures</h4>}
        {earlierCaptures.map(item => <InspectionPayload
          key={item.seq} title={`${item.type} · round ${item.round} · #${item.seq}`} value={item.payload} empty="This model request was not captured."
          note={captureNote(item)}
        ><CaptureIdentity item={item} /></InspectionPayload>)}
        {!current.model_requests?.length && <p className="inspection-source-note">No model request was captured for this delegation.</p>}
        <InspectionPayload title={current.result?.kind === "terminal_explanation" ? "Outcome" : "Assistant result"} value={current.result} empty="The assistant has not returned a result yet." />
        <details className="delegation-payload delegation-identifiers"><summary>Request identifiers</summary><dl><dt>Delegation</dt><dd>{current.delegation_id}</dd><dt>Voice session</dt><dd>{current.voice_session_id}</dd><dt>Chat</dt><dd>{current.chat_session_id}</dd><dt>Run</dt><dd>{current.run_id || "Not admitted yet"}</dd><dt>Source</dt><dd>{current.source}</dd></dl></details>
      </>}
    </div>
    <footer><button type="button" disabled={loading} onClick={() => refresh(value => value + 1)}>Refresh</button>{delegation.runId && <button type="button" onClick={() => viewChat(delegation.runId)}>View in chat</button>}</footer>
  </section>;
}
