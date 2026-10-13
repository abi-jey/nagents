import type { ResponseMetadata, ResponseUsage, RunUsage } from "./responseMetadata.js";
import { hasResponseDetails, hasVisibleUsage } from "./responseMetadata.js";
import { MessageContent } from "./MessageContent.js";

const outputMeaning: Record<ResponseUsage["reasoning_in_output"], string> = {
  included: "Reasoning tokens are included in Output; they are not added again.",
  separate: "The provider reports reasoning separately from Output. These counts are shown without calculating a new total.",
  unknown: "The provider does not report whether reasoning is included in Output. These counts are shown without calculating a new total.",
};
const outputLabel: Record<ResponseUsage["reasoning_in_output"], string> = {
  included: "in Output", separate: "separate", unknown: "Output inclusion unknown",
};
const tokens = (value: number) => value.toLocaleString("en-US");

function RunTokenBreakdown({ usage }: { usage: RunUsage }) {
  if (!hasVisibleUsage(usage)) return null;
  const partial = (reported: number) => reported < usage.response_count
    ? <span className="response-usage-meaning"> ({reported}/{usage.response_count} responses)</span> : null;
  return <div className="response-usage" aria-label="Provider-reported run tokens"
    title={`Provider-reported tokens for recorded responses in this run, separate from the Context estimate. ${outputMeaning[usage.reasoning_in_output]}`}>
    {usage.prompt_reported && <span>Input <b>{tokens(usage.prompt_tokens)}</b>{partial(usage.prompt_response_count)}</span>}
    {usage.completion_reported && <span>Output <b>{tokens(usage.completion_tokens)}</b>{partial(usage.completion_response_count)}</span>}
    {usage.reasoning_reported && <span title={outputMeaning[usage.reasoning_in_output]}>Reasoning <b>{tokens(usage.reasoning_tokens)}</b>{" "}<span className="response-usage-meaning">({outputLabel[usage.reasoning_in_output]})</span></span>}
  </div>;
}

export function ResponseDetails({ metadata, open, toggle, showRunUsage = false, runOpen = false, toggleRun }: {
  metadata?: ResponseMetadata;
  open: boolean;
  toggle: (open: boolean) => void;
  showRunUsage?: boolean;
  runOpen?: boolean;
  toggleRun?: (open: boolean) => void;
}) {
  if (!hasResponseDetails(metadata, showRunUsage) || !metadata) return null;
  const thinking = metadata.status === "thinking";
  const interrupted = ["cancelled", "failed"].includes(metadata.status);
  const interruption = interrupted && <> <span className="response-interrupted">{metadata.status === "cancelled" ? "Stopped" : "Interrupted"}</span></>;
  const run = showRunUsage && metadata.run_id && metadata.run_usage &&
    metadata.run_usage.response_count > 1 && metadata.run_usage.usage_response_count > 0 ? metadata.run_usage : undefined;
  return <div className="response-details" data-response-id={metadata.response_id}>
    {metadata.summary ? <details className="response-reasoning" data-disclosure-key={`reasoning:${metadata.response_id}`}
      open={open} onToggle={event => toggle(event.currentTarget.open)}>
      <summary><span className={thinking ? "response-thinking" : undefined} role={thinking ? "status" : undefined}>
        {thinking && <i aria-hidden="true" />}{thinking ? "Thinking" : "Reasoning summary"}
      </span>{interruption}</summary>
      <div className="response-reasoning-content"><p className="response-reasoning-note">Provider-supplied summary</p>
        <MessageContent text={metadata.summary} />
        {metadata.summary_truncated && <p className="response-reasoning-note">Summary shortened to the 16,384-character retention limit.</p>}
      </div>
    </details> : (thinking || metadata.summary_expired) && <span className="response-reasoning-meta">
      {thinking ? <span className="response-thinking" role="status"><i aria-hidden="true" />Thinking</span>
        : metadata.summary_expired && <span className="response-reasoning-expired" title="Summaries are retained for the latest 100 responses in this conversation. Usage is kept separately.">Summary no longer retained</span>}
      {interruption}
    </span>}
    {run && <details className="response-run-usage" data-disclosure-key={`run-usage:${metadata.response_session_id}:${metadata.run_id}`}
      open={runOpen} onToggle={event => toggleRun?.(event.currentTarget.open)}>
      <summary>Recorded run usage · {run.response_count} responses{!run.complete && " · partial"}</summary>
      <RunTokenBreakdown usage={run} />
      <p className="response-reasoning-note">Counts cover recorded responses. Attempts ending before response metadata arrived may be absent.</p>
      {!run.complete && <p className="response-reasoning-note">Input reported for {run.prompt_response_count} of {run.response_count} responses; Output for {run.completion_response_count} of {run.response_count}.</p>}
      {!run.reasoning_reported && run.reasoning_response_count > 0 && <p className="response-reasoning-note">Reasoning was reported for {run.reasoning_response_count} of {run.response_count} responses; a complete reasoning total is unavailable.</p>}
    </details>}
  </div>;
}
