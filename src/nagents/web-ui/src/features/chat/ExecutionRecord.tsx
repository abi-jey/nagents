import { CodeBlock } from "../../components/CodeBlock.js";
import type { Entry } from "./transcript.js";

export function ExecutionRecord({
  entry,
  open,
  toggle,
}: {
  entry: Entry;
  open?: boolean;
  toggle?: (open: boolean) => void;
}) {
  const name = entry.kind === "task"
    ? entry.trigger || (entry.recorded ? "Recorded trigger unavailable" : entry.followup ? "human" : "delegation")
    : entry.title || "Tool call";
  return (
    <details
      className="execution-record"
      data-disclosure-key={entry.id}
      open={open}
      onToggle={(event) => toggle?.(event.currentTarget.open)}
    >
      <summary>
        <span className="execution-summary">
          <span className="execution-name">
            {entry.kind === "task"
              ? `${entry.recorded ? "Latest known activation" : "Activation"} ${entry.activation || 0}: `
              : ""}
            <code>{name}</code>
            {entry.kind === "task" && !!entry.followup && (
              <span> / follow-up {entry.followup}</span>
            )}
          </span>
          <span className="execution-state" data-state={entry.state}>
            {entry.state === "Completed" && entry.title === "delegate"
              ? "Delegation request completed"
              : entry.state === "Completed" &&
                  ["schedule_wakeup", "wake_up_in"].includes(entry.title || "")
                ? "Scheduling request completed"
                : entry.state || "Recorded activity"}
          </span>
        </span>
      </summary>
      <div className="execution-detail">
        {entry.kind !== "context" && (
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
        )}
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
        {entry.inputs !== undefined && (
          <CodeBlock
            label={
              entry.kind === "task"
                ? entry.recorded
                  ? "Recorded task request"
                  : "Task request"
                : "Inputs"
            }
            text={entry.inputs}
          />
        )}
        {entry.text && (
          <CodeBlock
            label={
              entry.kind === "context"
                ? "Original saved content"
                : "Streamed output"
            }
            text={entry.text}
          />
        )}
        {entry.result !== undefined && (
          <CodeBlock
            label={
              entry.recorded
                ? entry.result
                  ? "Recorded result"
                  : "Recorded result (empty)"
                : entry.result
                  ? "Result"
                  : "Result (empty)"
            }
            text={entry.result}
          />
        )}
        {entry.error && <CodeBlock label="Error" text={entry.error} />}
        {entry.result === undefined && entry.kind !== "context" && (
          <p className="record-note">
            {entry.recorded
              ? "No final result in the registry snapshot."
              : "No final result received."}
          </p>
        )}
      </div>
    </details>
  );
}
