import { useId, useLayoutEffect, useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { MessageContent } from "./MessageContent.js";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { delegationPreview, type DelegationPresentation } from "./delegationFlow.js";

export function DelegationActivity({ item, open, toggle, inspect }: {
  item: DelegationPresentation; open: boolean; toggle: (open: boolean) => void; inspect?: (id: string) => void;
}) {
  const id = useId(), preview = delegationPreview(item);
  const tail = useRef<HTMLSpanElement>(null);
  const [overflow, setOverflow] = useState(false);
  useLayoutEffect(() => {
    const element = tail.current;
    if (!element) return;
    const measure = () => setOverflow((element.firstElementChild?.getBoundingClientRect().width || 0) > element.clientWidth + 1);
    const view = element.ownerDocument.defaultView;
    const observer = view && typeof view.ResizeObserver === "function" ? new view.ResizeObserver(measure) : undefined;
    observer?.observe(element); measure();
    return () => observer?.disconnect();
  }, [preview.text]);
  const label = `${item.profile}${item.name ? ` · ${item.name}` : ""}`;
  return <div className="delegation-activity" data-phase={item.phase} data-event={item.kind} data-task-id={item.taskId}>
    <button type="button" className="delegation-line" aria-expanded={open} aria-controls={`${id}-details`} onClick={() => toggle(!open)}
      aria-label={`${label}. ${item.kind === "result" ? "Result" : "Delegation"}. ${item.phase}. ${preview.tool ? `${preview.tool}: ` : ""}${preview.text}`}>
      <span className="delegation-mark" aria-hidden="true">{item.kind === "result" ? <Icon name={item.phase === "failed" || item.phase === "cancelled" ? "close" : "check"} size={13} /> : <Icon name="team" size={13} />}</span>
      <strong>{item.profile}</strong>{item.name && <span className="delegation-instance">{item.name}</span>}
      <span className={`delegation-preview${preview.tool ? " is-tool" : ""}`} aria-busy={preview.live}>
        {preview.tool && <code>{preview.tool}:</code>}<span className="delegation-tail" ref={tail} data-overflow={overflow} title={preview.text}><span key={preview.text}>{preview.text.replace(/\s+/g, " ").trim()}</span></span>
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
