import type { ResponseMetadata } from "./responseMetadata.js";
import { hasResponseDetails } from "./responseMetadata.js";
import { MessageContent } from "./MessageContent.js";
import { reasoningExcerpt, reasoningParts } from "./reasoningSummary.js";

export type ResponseDetailsProps = {
  metadata?: ResponseMetadata;
  open: boolean;
  toggle: (open: boolean) => void;
  hideSummary?: boolean;
};
export function ResponseDetails({ metadata, open, toggle, hideSummary = false }: ResponseDetailsProps) {
  if (!metadata) return null;
  const thinking = metadata.status === "thinking";
  const interrupted = ["cancelled", "failed"].includes(metadata.status);
  if (!interrupted && (hideSummary || !hasResponseDetails(metadata))) return null;
  const interruption = interrupted && <span className="response-interrupted" role="status">{metadata.status === "cancelled" ? "Stopped" : "Interrupted"}</span>;
  const { title, body } = reasoningParts(metadata.summary);
  const excerpt = reasoningExcerpt(body, title);
  const label = open || excerpt === title ? title : `${title}. ${excerpt}`;
  return <div className="response-details" data-response-id={metadata.response_id} data-phase={thinking ? "thinking" : interrupted ? metadata.status : "complete"}>
    {!hideSummary && metadata.summary ? <details className="response-reasoning" data-disclosure-key={`reasoning:${metadata.response_id}`}
      open={open} onToggle={event => { if (event.currentTarget.open !== open) toggle(event.currentTarget.open); }}>
      <summary aria-label={`${open ? "Hide" : "Show"} reasoning: ${label}`}>
        <span className={`response-reasoning-mark${thinking ? " response-thinking" : ""}`} aria-hidden="true"><i /><i /><i /></span>
        <span className="response-reasoning-title">{title}</span><span className="response-reasoning-preview">{excerpt}</span>{interruption}
        {thinking && <span className="sr-only" role="status">Thinking</span>}
      </summary>
      <div className="response-reasoning-content">
        {body ? <MessageContent text={body} /> : <p className="response-reasoning-note">{thinking ? "Waiting for the first summary words…" : "The provider supplied a heading without further summary text."}</p>}
        {metadata.summary_truncated && <p className="response-reasoning-note">Summary shortened to the 16,384-character retention limit.</p>}
      </div>
    </details> : ((!hideSummary && (thinking || metadata.summary_expired)) || interrupted) && <span className="response-reasoning-meta">
      {!hideSummary && (thinking ? <span className="response-thinking" role="status"><span className="response-reasoning-mark" aria-hidden="true"><i /><i /><i /></span><span className="sr-only">Thinking</span></span>
        : metadata.summary_expired && <span className="response-reasoning-expired" title="Summaries are retained for the latest 100 responses in this conversation. Usage is kept separately.">Summary no longer retained</span>
      )}
      {interruption}
    </span>}

  </div>;
}
