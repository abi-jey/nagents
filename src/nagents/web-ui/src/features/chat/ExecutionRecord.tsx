import { CodeBlock } from "../../components/CodeBlock";
import type { Entry } from "./transcript";

export function ExecutionRecord({ entry }: { entry: Entry }) {
  return (
    <details className="execution-record">
      <summary>
        <span className="execution-summary">
          <span className="execution-name">
            {entry.kind === "task" ? "Task: " : ""}
            <code>{entry.title || "Tool call"}</code>
          </span>
          <span className="execution-state" data-state={entry.state}>
            {entry.state || "Recorded activity"}
          </span>
        </span>
      </summary>
      <div className="execution-detail">
        <dl className="request-identity">
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
          {entry.durationMs !== undefined && (
            <>
              <dt>Duration</dt>
              <dd>{entry.durationMs.toFixed(0)} ms</dd>
            </>
          )}
        </dl>
        {entry.inputs !== undefined && (
          <CodeBlock
            label={entry.kind === "task" ? "Task request" : "Inputs"}
            text={entry.inputs}
          />
        )}
        {entry.text && <CodeBlock label="Streamed output" text={entry.text} />}
        {entry.result !== undefined && (
          <CodeBlock
            label={entry.result ? "Result" : "Result (empty)"}
            text={entry.result}
          />
        )}
        {entry.error && <CodeBlock label="Error" text={entry.error} />}
        {entry.result === undefined && (
          <p className="record-note">No final result received.</p>
        )}
      </div>
    </details>
  );
}
