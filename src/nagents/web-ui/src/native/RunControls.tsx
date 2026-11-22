import { activeRun, steeringTarget, type NativeClient } from "./client.js";
import type { SteeringStatus } from "./transport.js";

type View = ReturnType<NativeClient["snapshot"]>;
const steeringLabels: Record<SteeringStatus, string> = {
  pending: "Pending · not yet offered",
  offered: "Offered · persistence not confirmed",
  applied: "Applied · persistence acknowledged, not proof of model consumption",
  uncertain: "Uncertain · may be persisted; inspect the conversation, no replay",
  undelivered: "Undelivered · turn ended before offer; not queued or replayed",
};

export function RunControls({ client, view }: { client: NativeClient; view: View }) {
  const { queue, queueLimit, queueBusy, connected, session, steering, bindings, stopping } = view;
  const attempts = bindings.filter(binding => binding.session === session && binding.state !== "sending");
  const receipts = steering.flatMap(state => state.receipts);
  const submitting = bindings.some(binding => binding.session === session && binding.state === "sending");
  return <>
    {queue && <details className="native-controls native-queue">
      <summary>Input queue · {queue.entries.length} pending{queue.paused ? " · Paused" : ""}</summary>
      <div className="native-controls-body">
        <div className="native-controls-actions">
          <small>{queue.entries.length}/{queueLimit} · {queue.paused ? "Resume explicitly to dispatch." : "Runs in order after current work."}</small>
          <button type="button" disabled={queueBusy || !connected} onClick={() => void client.controlQueue(queue.paused ? "resume" : "pause")}>{queue.paused ? "Resume queue" : "Pause queue"}</button>
          <button type="button" disabled={queueBusy || !queue.entries.length || !connected} onClick={() => void client.controlQueue("clear")}>Clear pending &amp; pause</button>
        </div>
        {!!queue.entries.length && <ol className="native-queue-items">
          {queue.entries.map(entry => {
            const run = entry.run;
            return <li key={entry.run ? entry.run.id : entry.request_id}><pre>{entry.prompt}</pre>
              {run && <button type="button" disabled={!!stopping || queueBusy || !connected} onClick={() => void client.cancel(run.id)}>Remove</button>}</li>;
          })}
        </ol>}
      </div>
    </details>}
    {(!!steeringTarget(view) || !!receipts.length || !!attempts.length) && <details className="native-controls native-receipts">
      <summary>Input receipts{receipts.length ? ` · ${receipts.length} steering` : ""}{attempts.length ? " · Check submission" : ""}</summary>
      <div className="native-controls-body">
        {!attempts.length && !receipts.length && <p>Steering targets the active turn at its next persisted safe point. Send and Enter still use the input queue.</p>}
        {attempts.map(binding => <article className="native-receipt" key={binding.id}>
          <strong>{binding.state === "confirmed" ? "Host confirmed admission" : binding.state === "rejected" ? "Exact turn closed without admitting this input" : "Admission unconfirmed"}</strong>
          <small>{binding.run ? `Steering run ${binding.run}` : "Next-turn input"} · Request {binding.id}</small>
          <pre>{binding.prompt}</pre>
          {binding.state === "unconfirmed"
            ? <button type="button" disabled={!connected || submitting} onClick={() => void client.retry(binding.id)}>{binding.run ? "Check / retry exact run" : "Retry exact input"}</button>
            : <button type="button" onClick={() => client.release(binding.id)}>{binding.state === "confirmed" ? "Clear bound draft" : "Release rejected draft"}</button>}
        </article>)}
        {receipts.map(receipt => <article className="native-receipt" key={receipt.input_id}>
          <strong>#{receipt.position} · {steeringLabels[receipt.status]}</strong>
          <small>{receipt.run_id === activeRun(view)?.id ? "Active run" : "Previous run"} · {receipt.run_id}</small>
          <pre>{receipt.prompt}</pre>
        </article>)}
      </div>
    </details>}
  </>;
}
