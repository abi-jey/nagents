import type { ResponseMetadata } from "./responseMetadata.js";
import { hasResponseDetails } from "./responseMetadata.js";
import { MessageContent } from "./MessageContent.js";

export function ResponseDetails({ metadata, open, toggle, hideSummary = false }: {
  metadata?: ResponseMetadata;
  open: boolean;
  toggle: (open: boolean) => void;
  hideSummary?: boolean;
}) {
  if (!metadata) return null;
  const thinking = metadata.status === "thinking";
  const interrupted = ["cancelled", "failed"].includes(metadata.status);
  if (!interrupted && (hideSummary || !hasResponseDetails(metadata))) return null;
  const interruption = interrupted && <span className="response-interrupted" role="status">{metadata.status === "cancelled" ? "Stopped" : "Interrupted"}</span>;
  const preview = metadata.summary.split("\n").find(line => line.trim())?.replace(/^[#* _>-]+|[*_]+$/g, "").slice(0, 100);
  const headingOnly = !thinking && /^(?:#{1,6}[^\S\n]+[^\n]+|\*\*[^\n]+\*\*|__[^\n]+__)$/.test(metadata.summary.trim());
  return <div className="response-details" data-response-id={metadata.response_id}>
    {!hideSummary && metadata.summary ? <details className="response-reasoning" data-disclosure-key={`reasoning:${metadata.response_id}`}
      open={open} onToggle={event => toggle(event.currentTarget.open)}>
      <summary><span className={thinking ? "response-thinking" : undefined} role={thinking ? "status" : undefined}>
        {thinking && <i aria-hidden="true" />}{thinking ? "Thinking" : headingOnly ? "Summary heading" : "Reasoning summary"}
      </span>{interruption}{preview && <span className="response-reasoning-preview" aria-hidden="true">{preview}</span>}</summary>
      <div className="response-reasoning-content"><p className="response-reasoning-note">Provider-supplied summary</p>
        <MessageContent text={metadata.summary} />
        {headingOnly && <p className="response-reasoning-note">The provider supplied a heading without further summary text.</p>}
        {metadata.summary_truncated && <p className="response-reasoning-note">Summary shortened to the 16,384-character retention limit.</p>}
      </div>
    </details> : ((!hideSummary && (thinking || metadata.summary_expired)) || interrupted) && <span className="response-reasoning-meta">
      {!hideSummary && (thinking ? <span className="response-thinking" role="status"><i aria-hidden="true" />Thinking</span>
        : metadata.summary_expired && <span className="response-reasoning-expired" title="Summaries are retained for the latest 100 responses in this conversation. Usage is kept separately.">Summary no longer retained</span>
      )}
      {interruption}
    </span>}

  </div>;
}
