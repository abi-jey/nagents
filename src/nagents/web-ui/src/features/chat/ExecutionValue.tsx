import { useMemo, useState } from "react";
import { presentToolResult, toolResultTokens, visibleToolText, type ResultPresentation } from "./toolResultPresentation.js";

function ResultText({ text, structured }: { text: string; structured: boolean }) {
  const tokens = useMemo(() => structured ? toolResultTokens(text) : [{ text, kind: "plain" as const }], [text, structured]);
  return <code>{tokens.map((token, index) => token.kind === "plain" ? token.text
    : <span className={`tool-token-${token.kind}`} key={index}>{token.text}</span>)}</code>;
}

export function ExecutionValue({ label, text, emptyText = "No content returned.", formatOutput = false, toolName = "", preserveReadingSurface = false }: {
  label: string; text: string; emptyText?: string; formatOutput?: boolean; toolName?: string; preserveReadingSurface?: boolean;
}) {
  const [copy, setCopy] = useState({ text: "", status: "idle" });
  const [raw, setRaw] = useState(false);
  const [limit, setLimit] = useState(32 * 1024);
  const presentation = useMemo<ResultPresentation>(() => formatOutput ? presentToolResult(text, toolName)
    : { format: "Text", text, structured: false }, [text, toolName, formatOutput]);
  const canFormat = !!presentation.sections || presentation.text !== text || presentation.structured;
  const formatted = formatOutput && !raw;
  // A live stream can become a structured result while its text is being read.
  // Keep that original pre mounted; command sections are for fresh result views.
  const sections = formatted && !preserveReadingSurface ? presentation.sections : undefined;
  const value = formatted ? presentation.text : text;
  const visible = visibleToolText(value, limit);
  const sectionValues = sections?.map((section) => ({ ...section, ...visibleToolText(section.text, limit) }));
  const omitted = sectionValues ? sectionValues.reduce((total, section) => total + section.omitted, 0) : visible.omitted;
  const status = copy.text === text ? copy.status : "idle";
  return (
    <div className="tool-value tool-result-value">
      <div className="tool-value-toolbar">
        <span>{raw && canFormat ? "Raw result" : presentation.format}</span>
        <button type="button" disabled={!text} aria-label={`Copy ${label.toLowerCase()}`}
          title="Copy the original output, without display formatting"
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(text);
              setCopy({ text, status: "copied" });
            } catch { setCopy({ text, status: "failed" }); }
          }}>{status === "copied" ? "Copied" : "Copy"}</button>
        {canFormat && <button type="button" className="tool-result-raw" aria-pressed={raw}
          aria-label={`Show raw ${label.toLowerCase()}`} onClick={() => setRaw(!raw)}>Raw</button>}
      </div>
      {formatted && presentation.note && <p className="tool-result-note">{presentation.note}</p>}
      {!!presentation.indicators?.length && <div className="tool-result-indicators" aria-label="Reported command outcome">
        {presentation.indicators.map((indicator) => <span key={indicator.text} data-warning={indicator.warning || undefined}>{indicator.text}</span>)}
      </div>}
      {sectionValues ? <div className="tool-result-sections">
        {sectionValues.map((section) => <section key={section.label}>
          <h4>{section.label}</h4>
          <pre tabIndex={0} role="region" aria-label={`${label}: ${section.label.toLowerCase()}`}><code>{section.text || "(empty)"}</code></pre>
        </section>)}
      </div> : <pre tabIndex={0} role="region" aria-label={label}>
        <ResultText text={visible.text || (text ? "(empty string)" : emptyText)} structured={formatted && presentation.structured && !omitted} />
      </pre>}
      {!!omitted && <div className="tool-result-overflow">
        <span>Showing the beginning and end. {omitted.toLocaleString("en-US")} characters between them. Copy includes all output.</span>
        <button type="button" onClick={() => setLimit(limit + 32 * 1024)}>Show more</button>
      </div>}
      <span role="status" className={status === "failed" ? "tool-copy-error" : "sr-only"}>
        {status === "failed" ? "Could not copy. Select the text to copy it manually." : status === "copied" ? `${label} copied.` : ""}
      </span>
    </div>
  );
}
