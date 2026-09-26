import { useId, useRef, useState, type ReactNode } from "react";
import { Icon } from "../../components/Icon.js";
import { ExecutionValue } from "./ExecutionValue.js";
import { executionDuration, executionName, executionStatus, executionTarget } from "./executionPresentation.js";
import type { Entry } from "./transcript.js";

type Panel = { key: string; label: string; content: ReactNode };

export function ToolExecution({ entry, open, toggle, metadata, statusDescription }: {
  entry: Entry; open?: boolean; toggle?: (open: boolean) => void; metadata: ReactNode; statusDescription: string;
}) {
  const id = useId();
  const tabs = useRef<HTMLDivElement>(null);
  const separateResult = useRef(false);
  const [chosen, choose] = useState("");
  const name = entry.title || "Tool call";
  const target = executionTarget(entry);
  const status = executionStatus(entry);
  // Promote a matching stream in place, but never remove an existing result
  // surface. Its reading position must survive late output and tab switches too.
  const matchingOutput = !!entry.text && entry.text === entry.result;
  if (entry.result !== undefined && !matchingOutput) separateResult.current = true;
  const sameOutput = matchingOutput && !separateResult.current;
  const panels: Panel[] = [];
  if (entry.result !== undefined && !sameOutput) panels.push({
    key: "result", label: "Result", content: <ExecutionValue label="Result" text={entry.result} />,
  });
  if (entry.text) panels.push({
    key: "stream", label: sameOutput ? "Result" : "Output",
    content: <ExecutionValue label={sameOutput ? "Result" : "Streamed output"} text={entry.text} />,
  });
  if (entry.inputs !== undefined) panels.push({
    key: "inputs", label: "Inputs", content: <ExecutionValue label="Inputs" text={entry.inputs} />,
  });
  panels.push({ key: "metadata", label: "Details", content: <div className="tool-metadata">
    {entry.state === "Recorded result" && <p className="record-note">Result saved in conversation history. Original execution status and timing may be unavailable.</p>}
    {entry.delegatedTaskId && <p className="record-note">Delegated to <a href={`#${encodeURIComponent(`task-${entry.delegatedTaskId}`)}`}>{entry.delegatedTaskId}</a>. The task has its own execution state.</p>}
    {["schedule_wakeup", "wake_up_in"].includes(name) && <p className="record-note">This is the scheduling request, not evidence that a wake-up fired.</p>}
    {metadata}
  </div> });
  const selected = panels.some((panel) => panel.key === chosen) ? chosen : panels[0].key;
  return (
    <details className="execution-record tool-record" data-tone={status.tone} data-disclosure-key={entry.id} open={open}
      onToggle={(event) => {
        // Opening begins an inspection. New evidence must not change the tab the
        // reader is using; updates to a closed, untouched card may pick its result.
        if (event.currentTarget.open) choose((current) => current || selected);
        toggle?.(event.currentTarget.open);
      }}>
      <summary>
        <span className="tool-symbol"><Icon name={name === "channel_send" ? "channels" : "tools"} size={16} /></span>
        <span className="tool-heading">
          <span className="execution-name" title={name}>{executionName(name)}</span>
          {target && <span className="execution-target" title={target}>{target}</span>}
        </span>
        <span className="tool-outcome">
          <span className="execution-state" data-state={entry.state} title={statusDescription}>{status.label}</span>
          {entry.durationMs !== undefined && Number.isFinite(entry.durationMs) && entry.durationMs >= 0 &&
            <span className="execution-duration">{executionDuration(entry.durationMs)}</span>}
        </span>
        <span className="tool-chevron"><Icon name="chevron" size={14} /></span>
      </summary>
      <div className="tool-inspector">
        {entry.error && <div className="tool-error"><span>Error</span><pre tabIndex={0} role="region" aria-label="Error">{entry.error}</pre></div>}
        <div className="tool-tabs" role="tablist" aria-label={`${name} inspection`} ref={tabs}
          onKeyDown={(event) => {
            const buttons = [...(tabs.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]') || [])];
            const current = buttons.indexOf(event.target as HTMLButtonElement);
            if (current < 0) return;
            const index = event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1
              : event.key === "ArrowRight" ? (current + 1) % buttons.length
              : event.key === "ArrowLeft" ? (current + buttons.length - 1) % buttons.length : -1;
            if (index < 0) return;
            event.preventDefault();
            choose(panels[index].key);
            buttons[index].focus({ preventScroll: true });
            buttons[index].scrollIntoView?.({ block: "nearest", inline: "nearest" });
          }}>
          {panels.map((panel) => <button key={panel.key} type="button" role="tab" id={`${id}-tab-${panel.key}`}
            aria-selected={selected === panel.key} aria-controls={`${id}-panel-${panel.key}`}
            tabIndex={selected === panel.key ? 0 : -1} onClick={() => choose(panel.key)}>{panel.label}</button>)}
        </div>
        {panels.map((panel) => <div key={panel.key} className="tool-panel" role="tabpanel" data-panel={panel.key}
          id={`${id}-panel-${panel.key}`} aria-labelledby={`${id}-tab-${panel.key}`} hidden={selected !== panel.key}
          tabIndex={panel.key === "metadata" ? 0 : -1}>{panel.content}</div>)}
      </div>
    </details>
  );
}
