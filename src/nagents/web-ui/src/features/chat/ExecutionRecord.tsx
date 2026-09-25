import { ExecutionEvidence } from "./ExecutionEvidence.js";
import { executionDuration, executionTarget } from "./executionPresentation.js";
import type { Entry } from "./transcript.js";

export function ExecutionRecord({
  entry,
  open,
  toggle,
  disclosures = new Map<string, boolean>(),
  toggleDetail = () => undefined,
}: {
  entry: Entry;
  open?: boolean;
  toggle?: (open: boolean) => void;
  disclosures?: Map<string, boolean>;
  toggleDetail?: (key: string, open: boolean) => void;
}) {
  const name = entry.kind === "task"
    ? entry.trigger || (entry.recorded ? "Recorded trigger unavailable" : entry.followup ? "human" : "delegation")
    : entry.title || "Tool call";
  const target = executionTarget(entry);
  const state = entry.state === "Completed" && entry.title === "delegate" ? "Delegation request completed"
    : entry.state === "Completed" && ["schedule_wakeup", "wake_up_in"].includes(entry.title || "") ? "Scheduling request completed"
    : entry.state || "Recorded activity";
  const evidence = (slot: string, label: string, text: string) => (
    <ExecutionEvidence key={`${slot}:${entry.id}`} label={label} text={text} disclosureKey={`${slot}:${entry.id}`}
      open={disclosures.get(`${slot}:${entry.id}`)}
      toggle={(value) => toggleDetail(`${slot}:${entry.id}`, value)} />
  );
  const sameOutput = !!entry.text && entry.text === entry.result;
  const resultLabel = `${entry.recorded ? "Recorded result" : "Result"}${entry.result ? "" : " (empty)"}`;
  // Keep stream evidence in the same keyed sibling list, even when it becomes
  // the final result. Inserting a distinct result must not remount it either.
  const outputs = [
    ...(entry.result !== undefined && !sameOutput ? [evidence("result", resultLabel, entry.result)] : []),
    ...(entry.text ? [evidence("stream", sameOutput ? resultLabel : entry.kind === "context" ? "Original saved content" : "Streamed output", entry.text)] : []),
  ];
  const metadata = entry.kind !== "context" && (
    <details className="execution-metadata" data-disclosure-key={`metadata:${entry.id}`}
      open={disclosures.get(`metadata:${entry.id}`) ?? false}
      onToggle={(event) => toggleDetail(`metadata:${entry.id}`, event.currentTarget.open)}>
      <summary>Execution metadata</summary>
          <dl className="request-identity">
            {entry.kind === "tool" && (
              <><dt>Tool name</dt><dd>{name}</dd></>
            )}
            {entry.kind === "task" && (
              <>
                <dt>Activation</dt><dd>{entry.activation || 0}</dd>
                <dt>Trigger</dt><dd>{name}</dd>
              </>
            )}
            {entry.recordedStatus && (
              <>
                <dt>Recorded status</dt>
                <dd>{entry.recordedStatus}</dd>
              </>
            )}
            {entry.runId && (
              <>
                <dt>Run</dt>
                <dd>{entry.runId}</dd>
              </>
            )}
            {entry.kind === "tool" && entry.taskId && (
              <>
                <dt>Activation</dt>
                <dd>{entry.activation || 0}</dd>
              </>
            )}
            {entry.callId && (
              <>
                <dt>Call</dt>
                <dd>{entry.callId}</dd>
              </>
            )}
            {entry.taskId && (
              <>
                <dt>Task</dt>
                <dd>{entry.taskId}</dd>
              </>
            )}
            {entry.parentTaskId && (
              <>
                <dt>Parent task</dt>
                <dd>{entry.parentTaskId}</dd>
              </>
            )}
            {entry.depth !== undefined && (
              <>
                <dt>Depth</dt>
                <dd>{entry.depth}</dd>
              </>
            )}
            {!!entry.followup && (
              <>
                <dt>Follow-up</dt>
                <dd>{entry.followup}</dd>
              </>
            )}
            {entry.approval && (
              <>
                <dt>Approval</dt>
                <dd>{entry.approval}</dd>
              </>
            )}
            {entry.approvalId && (
              <>
                <dt>Approval ID</dt>
                <dd>{entry.approvalId}</dd>
              </>
            )}
            {entry.durationMs !== undefined && (
              <>
                <dt>Duration</dt>
                <dd>{entry.durationMs.toFixed(0)} ms</dd>
              </>
            )}
          </dl>
    </details>
  );
  return (
    <details className="execution-record" data-disclosure-key={entry.id} open={open}
      onToggle={(event) => toggle?.(event.currentTarget.open)}>
      <summary>
        <span className={`execution-summary${entry.kind === "tool" ? " execution-tool-summary" : ""}`}>
          <span className="execution-heading">
            <span className="execution-name" title={name}>
              {entry.kind === "task" ? `${entry.recorded ? "Latest known activation" : "Activation"} ${entry.activation || 0}: ` : ""}
              <code>{name}</code>
              {entry.kind === "task" && !!entry.followup && <span> / follow-up {entry.followup}</span>}
            </span>
            {target && <span className="execution-target" title={entry.inputs}>{target}</span>}
          </span>
          <span className="execution-outcome">
            <span className="execution-state" data-state={entry.state} title={state}>
              {state}
            </span>
            {entry.approval && <span className="execution-approval" title={`Approval: ${entry.approval}`}><span className="sr-only">Approval: </span>{entry.approval}</span>}
            {entry.durationMs !== undefined && Number.isFinite(entry.durationMs) && entry.durationMs >= 0 &&
              <span className="execution-duration">{executionDuration(entry.durationMs)}</span>}
          </span>
        </span>
      </summary>
      <div className="execution-detail">
        {entry.kind === "context" && (
          <p className="record-note">
            Saved message content, not a verified delivery or a new user
            command. This display does not establish task state or authority.
          </p>
        )}
        {entry.delegatedTaskId && (
          <p className="record-note">
            Delegated to{" "}
            <a href={`#${encodeURIComponent(`task-${entry.delegatedTaskId}`)}`}>
              {entry.delegatedTaskId}
            </a>
            . The task has its own execution state.
          </p>
        )}
        {["schedule_wakeup", "wake_up_in"].includes(entry.title || "") && (
          <p className="record-note">
            This is the scheduling request, not evidence that a wake-up fired.
          </p>
        )}
        {entry.kind === "task" && !entry.recorded && !entry.started && (
          <p className="record-note">
            Start event not recorded for this activation.
          </p>
        )}
        {entry.inputs !== undefined && evidence("inputs", entry.kind === "task" ? entry.recorded ? "Recorded task request" : "Task request" : "Inputs", entry.inputs)}
        {entry.error && evidence("error", "Error", entry.error)}
        {outputs}
        {entry.text && entry.text === entry.result && <p className="record-note">Streamed output matches the result above.</p>}
        {entry.result === undefined && entry.kind !== "context" && (
          <p className="record-note">
            {entry.recorded
              ? "No final result in the registry snapshot."
              : "No final result received."}
          </p>
        )}
        {metadata}
      </div>
    </details>
  );
}
