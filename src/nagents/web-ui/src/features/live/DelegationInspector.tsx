import { useEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { delegationDetails } from "./api.js";
import type { DelegationText, LiveDelegation, LiveDelegationDetails } from "./types.js";

function Payload({ title, value, empty }: { title: string; value?: DelegationText; empty: string }) {
  return <details className="delegation-payload">
    <summary>{title}<span>{value ? `${value.characters.toLocaleString()} characters` : "Not available yet"}</span></summary>
    {value ? <>{value.truncated && <p className="delegation-truncation">Preview truncated. Showing the first {Array.from(value.text).length.toLocaleString()} characters.</p>}<pre tabIndex={0}>{value.text || "(Empty text)"}</pre></> : <p>{empty}</p>}
  </details>;
}

export function DelegationInspector({ token, delegation, delegations, select, close, viewChat }: {
  token: string; delegation: LiveDelegation; delegations: LiveDelegation[];
  select(id: string): void; close(): void; viewChat(runId: string): void;
}) {
  const [details, setDetails] = useState<LiveDelegationDetails>();
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, refresh] = useState(0);
  const heading = useRef<HTMLHeadingElement>(null);
  useEffect(() => { heading.current?.focus({ preventScroll: true }); }, []);
  useEffect(() => {
    const abort = new AbortController();
    setLoading(true); setError("");
    void delegationDetails(token, delegation, AbortSignal.any([abort.signal, AbortSignal.timeout(12_000)]))
      .then(value => { if (!abort.signal.aborted) setDetails(value); })
      .catch((cause: unknown) => { if (!abort.signal.aborted) setError(cause instanceof Error ? cause.message : "Could not load delegation details."); })
      .finally(() => { if (!abort.signal.aborted) setLoading(false); });
    return () => abort.abort();
  }, [token, delegation.id, delegation.sessionId, delegation.chatSessionId, delegation.runId, delegation.seq, revision]);
  const current = details?.delegation_id === delegation.id && details.voice_session_id === delegation.sessionId && details.chat_session_id === delegation.chatSessionId && (!delegation.runId || details.run_id === delegation.runId) ? details : undefined;
  return <section className="delegation-inspector" aria-labelledby="delegation-inspector-title">
    <header><div><Icon name="channels" size={17} /><h3 id="delegation-inspector-title" ref={heading} tabIndex={-1}>Delegation details</h3></div><button type="button" aria-label="Close delegation details" onClick={close}><Icon name="close" /></button></header>
    <div className="delegation-inspector-body">
      <p className="delegation-inspector-intro">The request received by voice, the input sent to your assistant, and its processing events.</p>
      {delegations.length > 1 && <label className="delegation-request-picker">Request<select value={delegation.id} onChange={event => select(event.target.value)}>{delegations.map((item, index) => <option key={item.id} value={item.id}>Request {index + 1} · {item.agent} · {item.status}</option>)}</select></label>}
      <div className="delegation-target"><strong>{current?.agent || delegation.agent}</strong><span>{current?.provider || delegation.provider} · {current?.model || delegation.model}</span></div>
      {loading && <p className="delegation-inspector-notice" role="status">Updating request details…</p>}
      {error && <p className="delegation-inspector-error" role="alert">{error}</p>}
      {current && <>
        <h4>Processing</h4>
        {current.timeline_truncated && <p className="delegation-truncation">Showing the most recent events.</p>}
        <ol className="delegation-timeline">{current.timeline.map(event => <li key={event.seq} data-status={event.status}><span className="delegation-event-status">{event.status}<small>#{event.seq}</small></span><p>{event.text}</p><details><summary>Event payload</summary><pre tabIndex={0}>{JSON.stringify(event, null, 2)}</pre></details></li>)}</ol>
        <p className="delegation-source-note">Application lifecycle events. Tool execution and approvals are shown in the chat.</p>
        <Payload title="Voice request payload" value={current.request.transcript} empty="No request payload was captured." />
        <Payload title="Input sent to assistant" value={current.request.input} empty="The request has not been dispatched to the assistant." />
        <Payload title={current.result?.kind === "terminal_explanation" ? "Outcome" : "Assistant result"} value={current.result} empty="The assistant has not returned a result yet." />
        <details className="delegation-payload delegation-identifiers"><summary>Request identifiers</summary><dl><dt>Delegation</dt><dd>{current.delegation_id}</dd><dt>Voice session</dt><dd>{current.voice_session_id}</dd><dt>Chat</dt><dd>{current.chat_session_id}</dd><dt>Run</dt><dd>{current.run_id || "Not admitted yet"}</dd><dt>Source</dt><dd>{current.source}</dd></dl></details>
      </>}
    </div>
    <footer><button type="button" disabled={loading} onClick={() => refresh(value => value + 1)}>Refresh</button>{delegation.runId && <button type="button" onClick={() => viewChat(delegation.runId)}>View in chat</button>}</footer>
  </section>;
}
