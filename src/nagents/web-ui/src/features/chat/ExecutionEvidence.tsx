import { CodeBlock } from "../../components/CodeBlock.js";
import { useState } from "react";

export function ExecutionEvidence({ label, text, open, toggle, disclosureKey }: {
  label: string; text: string; open?: boolean; toggle: (open: boolean) => void; disclosureKey: string;
}) {
  // Size only chooses the initial state. Growing content must never replace a
  // focused node or override a reader's choice. The output region stays bounded.
  const [localOpen, setLocalOpen] = useState(() => text.length <= 2400 && text.split("\n", 25).length <= 24);
  const expanded = open ?? localOpen;
  return (
    <details className="execution-evidence" data-disclosure-key={disclosureKey} open={expanded}
      onToggle={(event) => {
        setLocalOpen(event.currentTarget.open);
        toggle(event.currentTarget.open);
      }}>
      <summary>{label} — {text.length.toLocaleString("en-US")} characters{!expanded && " · Expand full content"}</summary>
      <CodeBlock label={label} text={text} />
    </details>
  );
}
