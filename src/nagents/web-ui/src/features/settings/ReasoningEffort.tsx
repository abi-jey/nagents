import { useEffect, useId, useState, type ReactNode } from "react";
import { request } from "../../api/client.js";
import type { SettingsScope } from "./transport.js";
import { reasoningEfforts } from "./draft.js";

type OptionsReply = { options: string[] };
const labels: Record<string, string> = {
  "": "Provider default", default: "Provider default", none: "None", minimal: "Minimal",
  low: "Low", medium: "Medium", high: "High", xhigh: "Extra high", max: "Maximum",
};
export const effortLabel = (value = "") => labels[value] || value;

export function ReasoningEffort({ token, scope = "workspace", provider = "", model,
  value, update, disabled = false, error = "", defaultLabel = "Provider default", allowReset = false, children, id: suppliedId,
}: {
  token: string; scope?: SettingsScope; provider?: string; model: string;
  value: string; update: (value: string) => void; disabled?: boolean; error?: string;
  defaultLabel?: string; allowReset?: boolean; children?: ReactNode; id?: string;
}) {
  const generatedId = useId(), id = suppliedId || generatedId;
  const [result, setResult] = useState<OptionsReply>();
  const [failure, setFailure] = useState("");
  const [loading, setLoading] = useState(true);
  const query = new URLSearchParams({ scope, provider, model }).toString();
  useEffect(() => {
    const controller = new AbortController();
    setResult(undefined); setFailure(""); setLoading(true);
    const timer = setTimeout(() => {
      void request(`reasoning-options?${query}`, token, undefined, controller.signal).then(response => response.json()).then((reply: unknown) => {
        if (controller.signal.aborted) return;
        if (!reply || typeof reply !== "object" || !("options" in reply) || !Array.isArray(reply.options) ||
          !reply.options.every(option => typeof option === "string" && option && (reasoningEfforts as readonly string[]).includes(option))) {
          throw new Error("Invalid reasoning options from the server.");
        }
        setResult({ options: reply.options });
      }).catch(cause => {
        if (!controller.signal.aborted) setFailure(cause instanceof Error ? cause.message : "Could not load reasoning options.");
      }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    }, 200);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [query, token]);
  const explicit = !!value && value !== "default";
  const unsupported = explicit && !!result && !result.options.includes(value);
  const keepValue = explicit && !result?.options.includes(value);
  return <div className="settings-field reasoning-effort-field">
    <label htmlFor={id}>Reasoning effort</label>
    <select id={id} value={value} disabled={disabled} aria-invalid={!!error || unsupported}
      aria-describedby={`${id}-help${error || failure || unsupported ? ` ${id}-error` : ""}`}
      aria-busy={loading} onChange={event => update(event.target.value)}>
      <option value="">{defaultLabel}</option>
      {allowReset && <option value="default">Provider default</option>}
      {keepValue && <option value={value} disabled>{effortLabel(value)}{unsupported ? " · unavailable for this model" : " · saved"}</option>}
      {result?.options.map(option => <option key={option} value={option}>{effortLabel(option)}</option>)}
    </select>
    <p id={`${id}-help`}>{loading ? "Loading model options…" : result && !result.options.length
      ? "This model uses its provider’s default reasoning."
      : "Higher effort can take longer. Provider default sends no effort override."}{children}</p>
    {(error || failure || unsupported) && <p id={`${id}-error`} className="error-text" role="status">
      {error || failure || "Choose an effort supported by this model, or use the provider default."}
    </p>}
  </div>;
}
