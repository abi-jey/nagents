import { useMemo, useState } from "react";

export function ExecutionValue({ label, text }: { label: string; text: string }) {
  const [copy, setCopy] = useState({ text: "", status: "idle" });
  const format = useMemo(() => {
    try {
      const value: unknown = JSON.parse(text);
      if (value !== null && typeof value === "object") return "JSON";
    } catch { /* Plain output (including Python repr) stays exactly as received. */ }
    return "Text";
  }, [text]);
  const status = copy.text === text ? copy.status : "idle";
  return (
    <div className="tool-value">
      <div className="tool-value-toolbar">
        <span>{format}</span>
        <button type="button" disabled={!text} aria-label={`Copy ${label.toLowerCase()}`}
          onClick={async () => {
            try {
              await navigator.clipboard.writeText(text);
              setCopy({ text, status: "copied" });
            } catch { setCopy({ text, status: "failed" }); }
          }}>{status === "copied" ? "Copied" : "Copy"}</button>
      </div>
      <pre tabIndex={0} role="region" aria-label={label}><code>{text || "No content returned."}</code></pre>
      <span role="status" className={status === "failed" ? "tool-copy-error" : "sr-only"}>
        {status === "failed" ? "Could not copy. Select the text to copy it manually." : status === "copied" ? `${label} copied.` : ""}
      </span>
    </div>
  );
}
