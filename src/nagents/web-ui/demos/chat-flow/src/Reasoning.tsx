import { useId } from "react";
import { hasResponseDetails, type ResponseMetadata } from "../../../src/features/chat/responseMetadata.js";
import { MessageContent } from "../../../src/features/chat/MessageContent.js";
import { reasoningParts } from "./engine.js";
import { reasoningExcerpt } from "../../../src/features/chat/reasoningSummary.js";

export type Treatment = "inline" | "margin" | "excerpt";
export function Reasoning({ metadata, open, toggle }: { metadata: ResponseMetadata; open: boolean; toggle: (open: boolean) => void }) {
  const id = useId();
  if (!hasResponseDetails(metadata)) return null;
  const thinking = metadata.status === "thinking";
  const { title, body } = reasoningParts(metadata.summary);
  const excerpt = reasoningExcerpt(body, title);
  return <div className="thought" data-phase={thinking ? "thinking" : "complete"} data-open={open}>
    <button type="button" className="thought-toggle" aria-expanded={open} aria-controls={id}
      aria-label={`${open ? "Hide" : "Show"} reasoning: ${title}`} onClick={() => toggle(!open)}>
      <span className="thought-mark" aria-hidden="true"><i /><i /><i /></span>
      <span className="thought-title">{title}</span>
      <span className="thought-excerpt">{excerpt}</span>
    </button>
    <div id={id} className="thought-body" hidden={!open}>
      {body ? <MessageContent text={body} /> : <p className="thought-empty">Only a heading was supplied.</p>}
      {metadata.summary_truncated && <p className="thought-empty">Summary shortened.</p>}
    </div>
  </div>;
}
