import { useRef, useState } from "react";
import { Icon } from "../../components/Icon.js";
import { activityCategories, appearancePresets, defaultAppearance, saveAppearance, selectedAppearancePreset, useAppearance } from "./appearance.js";
import type { ActivityCategory, ActivityPlacement, DetailExpansion } from "./appearance.js";

const categoryNames: Record<ActivityCategory, string> = { shell: "Commands", files: "File changes", reasoning: "Reasoning summaries", agents: "Agents", notices: "Notices", tools: "Other tools" };
export function AppearanceSettings() {
  const value = useAppearance();
  const selected = selectedAppearancePreset(value);
  const [notice, setNotice] = useState("");
  const [customizing, setCustomizing] = useState(!selected);
  const visible = useRef<Partial<Record<ActivityCategory, ActivityPlacement>>>({});
  function update(next: typeof value) { setNotice(saveAppearance(next) ? "Saved in this browser" : "Applied for this page. Browser storage is unavailable."); }
  const position = appearancePresets.findIndex(preset => preset.id === selected?.id);
  return <section className="appearance-settings" aria-label="Chat appearance">
    <div className="appearance-heading"><div><h3>Conversation detail</h3><p>Choose how activity fits around your messages.</p></div><span className="appearance-location">This browser</span></div>
    <div className="appearance-preset-card">
      <div className="appearance-current"><strong>{selected?.label || "Custom"}</strong><span>Detail level</span></div>
      <input type="range" aria-label="Conversation detail level" aria-valuetext={selected?.label || "Custom"} min={0} max={appearancePresets.length - 1} step={1} value={position < 0 ? 2 : position}
        onChange={event => update(appearancePresets[event.target.valueAsNumber].value)} />
      <div className="appearance-levels">{appearancePresets.map(preset => <button type="button" key={preset.id} aria-pressed={selected?.id === preset.id} onClick={() => update(preset.value)}>{preset.label}</button>)}</div>
      <p className="appearance-description">{selected?.description || "Your own mix of visible activity, grouping, and expanded details."}</p>
      <div className="appearance-preview" aria-label="Presentation preview"><div><span className="appearance-preview-dot" /><span>Your answer stays here.</span></div>
        <div className="appearance-preview-activity"><Icon name="tools" size={13} /><span>{selected?.id === "messages" ? "Activity tucked away" : value.activity.tools.placement === "separate" ? "Activity gets its own space" : "Activity in compact groups"}</span><small>Details on demand</small></div></div>
    </div>
    <details className="appearance-advanced" open={customizing} onToggle={event => setCustomizing(event.currentTarget.open)}><summary>Customize activity</summary>
      <p className="appearance-description">Set visibility, placement, and the initial disclosure state for each kind of activity.</p>
      <div className="appearance-category-list">{activityCategories.map(category => {
        const current = value.activity[category], shown = current.placement !== "hidden";
        const change = (patch: Partial<typeof current>) => update({ ...value, activity: { ...value.activity, [category]: { ...current, ...patch } } });
        return <div className="appearance-category" key={category} data-hidden={!shown || undefined}>
          <label className="appearance-show"><input type="checkbox" aria-label={`Show ${categoryNames[category].toLowerCase()}`} checked={shown} onChange={event => {
            if (!event.target.checked) visible.current[category] = current.placement;
            change({ placement: event.target.checked ? visible.current[category] || "grouped" : "hidden" });
          }} /><span>{categoryNames[category]}</span></label>
          <label><span className="sr-only">{categoryNames[category]} placement</span><select aria-label={`${categoryNames[category]} placement`} disabled={!shown} value={shown ? current.placement : "grouped"}
            onChange={event => change({ placement: event.target.value as ActivityPlacement })}><option value="grouped">{category === "reasoning" ? "With answer" : "Grouped"}</option><option value="separate">Separate</option></select></label>
          <label><span className="sr-only">{categoryNames[category]} details</span><select aria-label={`${categoryNames[category]} details`} disabled={!shown} value={current.details}
            onChange={event => change({ details: event.target.value as DetailExpansion })}><option value="collapsed">Collapsed</option><option value="expanded">Expanded</option></select></label>
        </div>;
      })}</div>
      <label className="appearance-usage">Run statistics<select aria-label="Run statistics" value={value.runUsage} onChange={event => update({ ...value, runUsage: event.target.value as typeof value.runUsage })}>
        <option value="hidden">Hidden</option><option value="collapsed">Collapsed</option><option value="expanded">Expanded</option></select></label>
    </details>
    <footer className="appearance-footer"><span role="status">{notice || "Saved in this browser · applies to every conversation"}</span><button type="button" onClick={() => update(defaultAppearance)}>Reset appearance</button></footer>
    <p className="appearance-description">Approvals and errors remain visible at every level. These preferences change presentation only.</p>
  </section>;
}
