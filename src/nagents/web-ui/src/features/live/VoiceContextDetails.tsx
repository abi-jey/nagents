import { Icon } from "../../components/Icon.js";
import type { VoiceContext } from "./types.js";

export function VoiceContextDetails({ context, close }: { context: VoiceContext; close(): void }) {
  return <section className="voice-context-details" aria-labelledby="voice-context-title">
    <header><h3 id="voice-context-title" tabIndex={-1}>Voice context</h3><button type="button" onClick={close} aria-label="Collapse voice context"><Icon name="close" size={16} /></button></header>
    <p>{context.notice}</p>
    {!!context.message_count && <dl><dt>Starting context</dt><dd>{context.method === "generated_summary" ? "Prepared brief and recent conversation" : context.summary_included ? "Saved summary and recent conversation" : "Recent conversation"}</dd><dt>Included</dt><dd>{context.message_count} text items · {(context.bytes / 1000).toFixed(1)} KB of a 7 KB limit</dd></dl>}
    {context.omitted_content && <p>Some history or non-text content was left out. Ask your assistant for an earlier detail or a fuller recap when needed.</p>}
    {context.chat_session_id && <p className="voice-context-footnote">Your main assistant uses this chat’s saved history and summaries for requests that need more context. Change what voice starts with in Audio → Voice &amp; connection.</p>}
  </section>;
}
