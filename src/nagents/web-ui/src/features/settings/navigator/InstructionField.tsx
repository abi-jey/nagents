import { useId } from "react";

/** Missing prompt fields inherit; an explicit empty prompt stays custom. */
export function InstructionField({ label, value, custom, source, inheritLabel, scopeLabel, disabled, dirty, storagePath, sourcePath, description, maxLength, customize, inherit, change }: {
  label: string; value: string; custom: boolean; source: string; inheritLabel: string; scopeLabel: string;
  disabled: boolean; dirty: boolean; storagePath?: string; sourcePath?: string; description: string; maxLength?: number;
  customize: () => void; inherit: () => void; change: (text: string) => void;
}) {
  const id = useId();
  const editingScope = scopeLabel === "global" ? "global defaults" : scopeLabel === "workspace" ? "this workspace" : `agent ${scopeLabel}`;
  return <section className="an-instruction-field" data-custom={custom}>
    <div className="an-field-title"><label htmlFor={id}>{label}</label><span className="an-instruction-format">Markdown</span></div>
    <div className="an-instruction-mode" role="group" aria-label={`${label} mode`}>
      <button type="button" aria-pressed={!custom} disabled={disabled} onClick={inherit}>{inheritLabel}</button>
      <button type="button" aria-pressed={custom} disabled={disabled} onClick={customize}>Customize</button>
    </div>
    <p className="an-instruction-origin" id={`${id}-source`}>{custom ? scopeLabel === "global" ? "Global default" : `Custom · ${scopeLabel}` : `Inherited from ${source}`}{dirty && <span> · unsaved</span>}</p>
    <textarea id={id} aria-label={label} aria-describedby={`${id}-source ${id}-help`} rows={8} value={value} disabled={disabled} maxLength={maxLength} spellCheck={false} placeholder="Write a Markdown prompt…" onChange={event => change(event.target.value)} />
    <p className="an-footnote" id={`${id}-help`}>{description} {custom ? "An empty value is saved explicitly. Choose inheritance to remove this prompt and follow defaults again." : `Edit this text to customize ${editingScope}. Until you edit, this prompt follows changes to its source automatically.`}</p>
    {(storagePath || sourcePath) && <details className="an-instruction-storage"><summary>{custom ? "Markdown save location" : "Markdown source & save location"}</summary>
      {!custom && sourcePath && <p>Source <code>{sourcePath}</code></p>}
      {storagePath && <p>Customizations are saved in <code>{storagePath}</code></p>}
    </details>}
  </section>;
}
