import type { ReactNode } from "react";
import type { DelegationText } from "./types.js";

export function InspectionText({ value }: { value: DelegationText }) {
  return <>
    {value.truncated && <p className="delegation-truncation">{value.characters_complete === false
      ? `Partial capture. ${Array.from(value.text).length.toLocaleString()} characters retained; the full size was not recorded.`
      : `Preview truncated. Showing the first ${Array.from(value.text).length.toLocaleString()} of ${value.characters.toLocaleString()} characters.`}</p>}
    <pre tabIndex={0}>{value.text || "(Empty text)"}</pre>
  </>;
}

export function InspectionPayload({ title, value, empty, note, children }: {
  title: string; value?: DelegationText; empty: string; note?: string; children?: ReactNode;
}) {
  return <details className="delegation-payload inspection-payload">
    <summary><span className="inspection-payload-title">{title}</span><span>{value ? `${value.characters.toLocaleString()} ${value.characters_complete === false ? "captured characters" : "characters"}${value.truncated ? " · partial" : ""}` : "Not captured"}</span></summary>
    {note && <p className="inspection-source-note">{note}</p>}
    {value ? <InspectionText value={value} /> : <p>{empty}</p>}
    {children}
  </details>;
}
