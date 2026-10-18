import type { ResponseMetadata, ResponseUsage, RunUsage } from "./responseMetadata.js";
import { hasVisibleUsage } from "./responseMetadata.js";

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

export function RunDetails({ metadata, open, toggle }: {
  metadata: ResponseMetadata; open: boolean; toggle: (open: boolean) => void;
}) {
  const run = metadata.run_usage;
  if (!metadata.run_id || !run || run.response_count <= 1 || !run.usage_response_count) return null;
  return <details className="response-run-usage" data-run-id={metadata.run_id} data-record-key={`run-usage:${metadata.response_session_id}:${metadata.run_id}`} aria-label="Run statistics" data-disclosure-key={`run-usage:${metadata.response_session_id}:${metadata.run_id}`}
      open={open} onToggle={event => toggle(event.currentTarget.open)}>
      <summary>Recorded run usage · {run.response_count} responses{!run.complete && " · partial"}</summary>
      <RunTokenBreakdown usage={run} />
      <p className="response-reasoning-note">Counts cover recorded responses. Attempts ending before response metadata arrived may be absent.</p>
      {!run.complete && <p className="response-reasoning-note">Input reported for {run.prompt_response_count} of {run.response_count} responses; Output for {run.completion_response_count} of {run.response_count}.</p>}
      {!run.reasoning_reported && run.reasoning_response_count > 0 && <p className="response-reasoning-note">Reasoning was reported for {run.reasoning_response_count} of {run.response_count} responses; a complete reasoning total is unavailable.</p>}
    </details>;
}
