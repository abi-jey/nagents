import { useId } from "react";
import { Icon } from "../../components/Icon.js";
import { MessageContent } from "./MessageContent.js";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { delegationPreview, type DelegationPresentation } from "./delegationFlow.js";
import { delegationRelayState, relayAppears } from "./relayActivity.js";
import { RelayActivityMark } from "./RelayActivityMark.js";
import { SlidingActivityText } from "./SlidingActivityText.js";

export function DelegationActivity({ item, open, toggle, inspect }: {
  item: DelegationPresentation; open: boolean; toggle: (open: boolean) => void; inspect?: (id: string) => void;
}) {
  const id = useId(), preview = delegationPreview(item);
  const relayState=delegationRelayState(item);
  const latest=item.latest;
  const activity=item.kind==="result"?"result":latest
    ? `${latest.kind}:${latest.responseId || (latest.generationId ? `${latest.generationId}:${latest.toolIndex ?? 0}` : latest.callId || latest.id)}` : "assignment";
  const label = `${item.profile}${item.name ? ` · ${item.name}` : ""}`;
  return <div className="delegation-activity" data-phase={item.phase} data-event={item.kind} data-task-id={item.taskId}>
    <button type="button" className="delegation-line" aria-expanded={open} aria-controls={`${id}-details`} onClick={() => toggle(!open)}
      aria-label={`${label}. ${item.kind === "result" ? "Result" : "Delegation"}. ${item.phase}. ${preview.tool ? `${preview.tool}: ` : ""}${preview.text}`}>
      <RelayActivityMark state={relayState} appear={relayAppears(item)} />
      <strong>{item.profile}</strong>{item.name && <span className="delegation-instance">{item.name}</span>}
      <span className={`delegation-preview${preview.tool ? " is-tool" : ""}`} aria-busy={preview.live}>
        {preview.tool && <code>{preview.tool}:</code>}<SlidingActivityText text={preview.text} activity={activity} tool={!!preview.tool} />
      </span>
      <Icon name="chevron" size={11} />
    </button>
    <section id={`${id}-details`} className="delegation-details" hidden={!open} aria-label={`${label} details`}>
      <header><span>{label}</span>{inspect && item.taskId && <button type="button" className="text-button" onClick={() => inspect(item.taskId)}>Open conversation<Icon name="chat" size={13} /></button>}</header>
      <dl><dt>State</dt><dd>{item.phase}</dd><dt>Assigned task</dt><dd>{item.prompt}</dd></dl>
      {item.kind === "result" ? <MessageContent text={item.text || "No result text was recorded."} /> : item.latest?.kind === "tool" ? <ExecutionRecord entry={item.latest} open /> : preview.text && <MessageContent text={preview.text} />}
      {item.lastTool && (item.kind === "result" || item.latest?.kind !== "tool") && <ExecutionRecord entry={item.lastTool} open />}
      <details className="delegation-evidence"><summary>Request &amp; records <span>{item.sources.length}</span></summary>{item.sources.map(source => <section key={source.id}><strong>{source.kind === "tool" ? "Delegate call" : source.kind === "context" ? "Saved context" : source.kind === "notification" ? "Completion delivery" : "Task lifecycle"}</strong><pre>{JSON.stringify(source, null, 2)}</pre></section>)}</details>
    </section>
  </div>;
}
