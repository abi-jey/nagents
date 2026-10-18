import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type { KeyboardEvent } from "react";
import { createPortal } from "react-dom";
import { Icon } from "../../../components/Icon.js";
import type { IconName } from "../../../components/Icon.js";
import { ProviderLogo } from "../../../components/ProviderLogo.js";
import { AudioDeviceSettings } from "../../live/AudioDeviceSettings.js";
import type { AudioDeviceSelection, LivePhase, LiveTransport } from "../../live/types.js";
import { AgentModelDialog } from "../../team/AgentModelDialog.js";
import type { AgentModelReply } from "../../team/AgentModelDialog.js";
import { SettingsDialog } from "../SettingsDialog.js";
import { AppearanceSettings } from "../AppearanceSettings.js";
import { useSettings } from "../useSettings.js";
import type { ModelBinding, SettingsReply } from "../types.js";
import { PreferenceEditor } from "./PreferenceEditor.js";
import { VoiceEditor } from "./VoiceEditor.js";
import { announceSettingsSaved, nestedVoice, readJson, scopeName, settingsChanged, sourceLabel, targetKey, voiceOrigin } from "./state.js";
import type { AgentProfile, AgentSettingsReply, NavigatorRequest, NavigatorSection, NavigatorTarget, NavigatorVoiceReply } from "./state.js";

export interface NavigatorAudio {
  phase: LivePhase; transport?: LiveTransport; active: boolean; disabled: boolean;
  selection: AudioDeviceSelection; voice: string; model: string;
  applyDevice?: (kind: "input" | "output", id: string) => Promise<void>;
}
const sections: NavigatorSection[] = ["Overview", "Model", "Instructions", "Tools", "Voice"];
const icons: Record<NavigatorSection, IconName> = { Overview: "settings", Model: "plug", Instructions: "file-read", Tools: "tools", Voice: "wave" };
interface ModelSummary { binding: ModelBinding; source: string; provider: string; }

/** The live controller belongs to the composer; this surface only receives its
 * device operation. Closing or navigating settings cannot stop its media. */
export function AgentNavigator({ request: opening, token, audio, close, changed, acceptSettings, catalogRevision = "", workspaceName = "Workspace" }: {
  request?: NavigatorRequest; token: string; audio?: NavigatorAudio; close: () => void; changed: () => void; acceptSettings?: (reply: SettingsReply) => void; workspaceName?: string; catalogRevision?: string;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const title = useRef<HTMLHeadingElement>(null);
  const [target, setTarget] = useState<NavigatorTarget>({ scope: "workspace", agent: "" });
  const [section, setSection] = useState<NavigatorSection>("Overview");
  const [appearanceOpen, setAppearanceOpen] = useState(false);
  const [visited, setVisited] = useState<NavigatorTarget[]>([]);
  const [profiles, setProfiles] = useState<AgentProfile[]>([]);
  const [drafts, setDrafts] = useState<Record<string, Partial<Record<NavigatorSection, boolean>>>>({});
  const tabs = useRef<HTMLDivElement>(null);
  const id = useId();
  const opened = !!opening;
  const select = useCallback((next: NavigatorTarget, nextSection?: NavigatorSection) => {
    setAppearanceOpen(false);
    setTarget(next);
    if (nextSection) setSection(nextSection);
    setVisited(current => current.some(item => targetKey(item) === targetKey(next)) ? current : [...current, next]);
  }, []);
  useEffect(() => { if (opening) select(opening, opening.section); }, [opening?.serial, select]);
  useLayoutEffect(() => {
    const element = dialog.current;
    if (!opened || !element) return;
    const previous = document.activeElement;
    const overflow = document.documentElement.style.overflow;
    document.documentElement.style.overflow = "hidden";
    element.showModal(); title.current?.focus();
    return () => {
      element.close(); document.documentElement.style.overflow = overflow;
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus({ preventScroll: true });
    };
  }, [opened]);
  const report = useCallback((key: string, item: NavigatorSection, dirty: boolean) => setDrafts(current => current[key]?.[item] === dirty ? current : { ...current, [key]: { ...current[key], [item]: dirty } }), []);
  const catalog = useCallback((items: AgentProfile[]) => setProfiles(current => JSON.stringify(current) === JSON.stringify(items) ? current : items), []);
  useEffect(() => {
    if (!opening) return;
    const abort = new AbortController();
    // Reopening and the existing Team catalog stream trigger reads, never writes.
    // A retained dirty editor keeps its original revision and draft.
    window.dispatchEvent(new window.Event(settingsChanged));
    void readJson<AgentSettingsReply>("agent-settings?scope=workspace", token, abort.signal)
      .then(reply => { if (!abort.signal.aborted) catalog(reply.profiles); })
      .catch(() => { /* The visible scoped editor reports its own read failure. */ });
    return () => abort.abort();
  }, [opening?.serial, catalogRevision, token, catalog]);
  function navigate(event: KeyboardEvent<HTMLDivElement>) {
    const index = sections.indexOf(section);
    const next = event.key === "ArrowRight" ? (index + 1) % sections.length : event.key === "ArrowLeft" ? (index + sections.length - 1) % sections.length : event.key === "Home" ? 0 : event.key === "End" ? sections.length - 1 : -1;
    if (next < 0) return;
    event.preventDefault(); setSection(sections[next]); tabs.current?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus();
  }
  const key = targetKey(target);
  const unsaved = Object.values(drafts).reduce((count, items) => count + Object.values(items).filter(Boolean).length, 0);
  const catalogAgents = profiles.some(profile => profile.id === "assistant") ? profiles : [{ id: "assistant", name: "assistant", description: "Default conversational agent", mode: "build", source: "" }, ...profiles];
  const agents = [...catalogAgents, ...visited.filter(item => item.scope === "agent" && !catalogAgents.some(profile => profile.id === item.agent)
    && Object.values(drafts[targetKey(item)] || {}).some(Boolean)).map(item => ({ id: item.agent, name: `${item.agent} (removed)`, description: "This definition is no longer available. Your draft is kept for inspection.", mode: "", source: "" }))];
  const name = target.scope === "workspace" ? workspaceName : scopeName(target);
  function node(item: NavigatorTarget, label: string, icon: IconName, detail = "") {
    const itemKey = targetKey(item), dirty = Object.values(drafts[itemKey] || {}).some(Boolean);
    return <button type="button" className={`an-node${!appearanceOpen && key === itemKey ? " selected" : ""}`} aria-current={!appearanceOpen && key === itemKey ? "page" : undefined} onClick={() => select(item)}><Icon name={icon} size={16} /><span>{label}{detail && <small>{detail}</small>}</span>{dirty && <span className="an-dirty-dot" aria-label="Unsaved changes" />}</button>;
  }
  return createPortal(<dialog ref={dialog} className="agent-navigator-dialog" id="agent-navigator-dialog" aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`} onCancel={event => { event.preventDefault(); close(); }}>
    <div className="agent-navigator">
      <nav className="an-navigation" aria-label="Settings hierarchy"><div className="an-navigation-heading"><Icon name="settings" size={16} /><h2>Settings</h2></div>
        {node({ scope: "global", agent: "" }, "Global defaults", "channels")}
        <div className="an-workspace-tree">{node({ scope: "workspace", agent: "" }, workspaceName, "folder", "Workspace")}
          <div className="an-agent-tree">{agents.map(profile => <div key={profile.id}>{node({ scope: "agent", agent: profile.id }, profile.name || profile.id, "team")}</div>)}</div>
        </div>
        <p className="an-navigation-note">Defaults flow down.<br />Customize what needs to differ.</p>
        <div className="an-appearance-link"><button type="button" className={`an-node${appearanceOpen ? " selected" : ""}`} aria-current={appearanceOpen ? "page" : undefined} onClick={() => setAppearanceOpen(true)}><Icon name="settings" size={16} /><span>Appearance<small>This browser</small></span></button></div>
        {unsaved > 0 && <p className="an-drafts-note" role="status">{unsaved} unsaved {unsaved === 1 ? "section" : "sections"}. Drafts stay here while you navigate or close settings.</p>}
      </nav>
      <div className="an-content">
        <header className="an-header"><div><span className="an-eyebrow">{appearanceOpen ? "Personal preferences" : target.scope === "agent" ? `Agent · ${workspaceName}` : target.scope === "global" ? "All workspaces" : "Workspace settings"}</span><h2 ref={title} tabIndex={-1} id={`${id}-title`}><Icon name={appearanceOpen ? "settings" : target.scope === "agent" ? "team" : target.scope === "global" ? "channels" : "folder"} size={23} />{appearanceOpen ? "Appearance" : name}</h2><p id={`${id}-description`}>{appearanceOpen ? "Shape how you read conversations. Model and agent settings stay the same." : target.scope === "agent" ? agents.find(profile => profile.id === target.agent)?.description || "Agent configuration and inherited defaults." : target.scope === "global" ? "The starting point for every workspace and agent." : "Defaults for agents in this workspace."}</p></div><button type="button" className="an-close" aria-label="Close agent settings" onClick={close}><Icon name="close" /></button></header>
        <div className="an-tabs" hidden={appearanceOpen} role="tablist" aria-label={`${name} settings sections`} ref={tabs} onKeyDown={navigate}>{sections.map(item => <button type="button" key={item} id={`${id}-${item}-tab`} role="tab" aria-selected={section === item} aria-controls={`${id}-panel-${key}`} tabIndex={section === item ? 0 : -1} onClick={() => setSection(item)}>{item === "Voice" && <Icon name="wave" size={14} />}{item}{drafts[key]?.[item] && <span className="an-dirty-dot" aria-label="Unsaved changes" />}</button>)}</div>
        {appearanceOpen && <div className="appearance-scroll"><AppearanceSettings /></div>}
        {visited.map(item => <TargetPane key={targetKey(item)} target={item} active={opened && !appearanceOpen && targetKey(item) === key} section={section} id={`${id}-panel-${targetKey(item)}`} labelledBy={`${id}-${section}-tab`} token={token}
          audio={audio} catalog={catalog} report={report} select={select} openSection={setSection} changed={changed} acceptSettings={acceptSettings} />)}
      </div>
    </div>
  </dialog>, document.body);
}

function TargetPane({ target, active, section, id, labelledBy, token, audio, catalog, report, select, openSection, changed, acceptSettings }: {
  target: NavigatorTarget; active: boolean; section: NavigatorSection; id: string; labelledBy: string; token: string; audio?: NavigatorAudio;
  catalog: (profiles: AgentProfile[]) => void; report: (key: string, section: NavigatorSection, dirty: boolean) => void;
  select: (target: NavigatorTarget, section?: NavigatorSection) => void; openSection: (section: NavigatorSection) => void; changed: () => void; acceptSettings?: (reply: SettingsReply) => void;
}) {
  const scroll = useRef<HTMLDivElement>(null);
  const [preferences, setPreferences] = useState<AgentSettingsReply>();
  const [voice, setVoice] = useState<NavigatorVoiceReply>();
  const [model, setModel] = useState<ModelSummary>();
  const [modelError, setModelError] = useState("");
  const [modelVisited, setModelVisited] = useState(section === "Model" || section === "Overview");
  const key = targetKey(target);
  const reportPreferences = useCallback((item: NavigatorSection, dirty: boolean) => report(key, item, dirty), [key, report]);
  const reportModel = useCallback((dirty: boolean) => report(key, "Model", dirty), [key, report]);
  const reportVoice = useCallback((dirty: boolean) => report(key, "Voice", dirty), [key, report]);
  const receivePreferences = useCallback((reply: AgentSettingsReply) => { setPreferences(reply); catalog(reply.profiles); }, [catalog]);
  const agentModel = useCallback((reply: AgentModelReply) => setModel({ binding: reply.effective, source: reply.source === "agent" && reply.model === null ? "definition" : reply.source, provider: reply.effective.provider }), []);
  const scopeModel = useCallback((reply: SettingsReply) => setModel({ binding: reply.model_binding || { provider: reply.connection.provider_id || "", id: reply.values.model }, source: reply.model_binding_source || "startup", provider: reply.connection.provider }), []);
  useEffect(() => {
    if (modelVisited) return;
    let abort: AbortController;
    const load = () => {
      abort?.abort(); abort = new AbortController();
      const signal = abort.signal;
      const reading = target.scope === "agent"
        ? readJson<AgentModelReply>(`agent-models/${encodeURIComponent(target.agent)}`, token, signal).then(reply => { if (!signal.aborted) agentModel(reply); })
        : readJson<SettingsReply>(target.scope === "global" ? "settings/global" : "settings", token, signal).then(reply => { if (!signal.aborted) scopeModel(reply); });
      void reading.then(() => { if (!signal.aborted) setModelError(""); }).catch(cause => {
        if (!signal.aborted) setModelError(cause instanceof Error ? cause.message : "Could not read the conversation model.");
      });
    };
    load(); window.addEventListener(settingsChanged, load);
    return () => { abort?.abort(); window.removeEventListener(settingsChanged, load); };
  }, [key, token, modelVisited, agentModel, scopeModel]);
  const savedModel = useCallback(() => { announceSettingsSaved(); changed(); }, [changed]);
  useEffect(() => { if (active && (section === "Model" || section === "Overview")) setModelVisited(true); }, [active, section]);
  useEffect(() => { if (active && scroll.current) scroll.current.scrollTop = 0; }, [active, section]);
  const live = voice && nestedVoice(voice);
  const overview: { section: NavigatorSection; value: string; source: string }[] = [
    { section: "Model", value: model?.binding.id || "Open model settings", source: model?.source || "" },
    { section: "Instructions", value: preferences ? preferences.values.instructions || "Empty prompt" : "Loading prompt…", source: preferences?.origins.instructions || "" },
    { section: "Tools", value: preferences ? `${preferences.effective_tools?.length ?? preferences.values.tools.length} available tools` : "Loading tools…", source: preferences?.origins.tools || "" },
    { section: "Voice", value: live ? live.enabled ? `${live.voice} · ${live.model.id}` : "Live voice disabled" : "Loading voice…", source: voice ? voiceOrigin(voice, live?.enabled ? "voice" : "enabled") : "" },
  ];
  return <div className="an-target" hidden={!active} id={id} role="tabpanel" aria-labelledby={labelledBy} tabIndex={0}>
    <div className={`an-scroll${section === "Model" && target.scope !== "agent" ? " an-wide-model" : ""}`} ref={scroll}>
      <div className="an-form">
        {target.scope === "global" && preferences?.managed_defaults && <p className="an-footnote" role="note">{preferences.managed_defaults.auto_update ? "Automatic updates are enabled for the global assistant. Bundled updates replace this file at startup with a warning. Saving changes disables automatic updates for the global assistant." : "Automatic updates are disabled for the global assistant. Saved customizations are preserved when bundled defaults change."}</p>}
        <div hidden={section !== "Overview"}><div className="an-section-heading"><div><h3>{target.scope === "agent" ? "Agent overview" : "Defaults overview"}</h3><p>Saved effective settings. Section badges identify unsaved drafts.</p></div></div>
          <div className="an-overview-list">{overview.map(item => <button type="button" key={item.section} onClick={() => openSection(item.section)}><Icon name={icons[item.section]} size={17} /><span><strong>{item.section}</strong><span>{item.value}</span><small>{sourceLabel(item.source)}</small></span><Icon name="chevron" size={13} /></button>)}</div>
          {preferences?.source_path && <details className="an-source"><summary>Agent Markdown file</summary><code>{preferences.source_path}</code></details>}
        </div>
        <div hidden={section !== "Model"}>{modelVisited && (target.scope === "agent" ? <><div className="an-section-heading"><div><h3>Conversation model</h3><p>Provider connection and model are saved as one binding.</p></div></div><AgentModelDialog embedded token={token} agent={target.agent} close={() => {}} changed={savedModel} onDraftChange={reportModel} onSnapshot={agentModel} /></> : <ScopeModel token={token} scope={target.scope} changed={savedModel} report={reportModel} snapshotChanged={scopeModel} acceptSettings={acceptSettings} openGlobal={() => select({ scope: "global", agent: "" }, "Model")} />)}</div>
        <div hidden={section !== "Instructions" && section !== "Tools"}><PreferenceEditor token={token} target={target} section={section} report={reportPreferences} snapshotChanged={receivePreferences} /></div>
        <div hidden={section !== "Voice"}><VoiceEditor token={token} target={target} report={reportVoice} snapshotChanged={setVoice} connections={() => select({ scope: "workspace", agent: "" }, "Model")} /></div>
      </div>
      <aside className="an-summary" aria-label="Saved effective settings and audio devices"><h3>Saved effective settings</h3><p className="an-summary-description">{target.scope === "agent" ? "What this agent uses for new activations." : "Defaults at this scope."}</p>
        <dl><div><dt>Conversation model</dt><dd><span>{model?.binding.id || modelError || "Loading model…"}{model?.binding.provider && <span className="an-summary-connection"><ProviderLogo provider={model.provider} />{model.binding.provider}</span>}</span><small>{sourceLabel(model?.source || "")}</small></dd></div>
          <div><dt>Tools</dt><dd><span>{preferences?.effective_tools?.length ?? preferences?.values.tools.length ?? "…"} available</span><small>{sourceLabel(preferences?.origins.tools || "")}</small></dd></div>
          <div><dt>Voice</dt><dd><span>{live ? live.enabled ? live.voice : "Disabled" : "…"}</span><small>{sourceLabel(voice ? voiceOrigin(voice, live?.enabled ? "voice" : "enabled") : "")}</small></dd></div>
          <div><dt>Speech model</dt><dd><span>{live?.model.id || "…"}<span className="an-summary-connection">{live?.model.provider || "Default connection"}</span></span><small>{sourceLabel(voice ? voiceOrigin(voice, "model") : "")}</small></dd></div></dl>
        <h3>Current voice conversation</h3><p className="an-session-note">{audio?.active ? `Connected · ${audio.voice} · ${audio.model}. Saved changes apply to the next conversation.` : audio && ["permission", "connecting", "ending"].includes(audio.phase) ? `Voice is ${audio.phase}.` : "No active voice conversation."}</p>
        {active && <AudioDeviceSettings transport={audio?.transport} disabled={audio?.disabled} active={audio?.active} activeSelection={audio?.selection} applyDevice={audio?.applyDevice} />}
      </aside>
    </div>
  </div>;
}

function ScopeModel({ token, scope, changed, report, snapshotChanged, openGlobal, acceptSettings }: {
  token: string; scope: "global" | "workspace"; changed: () => void; report: (dirty: boolean) => void;
  snapshotChanged: (reply: SettingsReply) => void; openGlobal: () => void; acceptSettings?: (reply: SettingsReply) => void;
}) {
  const settings = useSettings({ token, blocked: false, operate: async action => { await action(); return true; }, accept: reply => acceptSettings?.(reply) });
  useEffect(() => { if (scope === "global") settings.showGlobal(); else settings.show(); return settings.close; }, []);
  useEffect(() => { if (settings.snapshot) snapshotChanged(settings.snapshot); }, [settings.snapshot, snapshotChanged]);
  useEffect(() => { if (settings.notice) changed(); }, [settings.notice, changed]);
  useEffect(() => {
    const refreshClean = () => { if (!settings.dirty && !settings.pending && !settings.needsRefresh) void settings.refresh(); };
    window.addEventListener(settingsChanged, refreshClean);
    return () => window.removeEventListener(settingsChanged, refreshClean);
  }, [settings.dirty, settings.pending, settings.needsRefresh]);
  return <SettingsDialog embedded settings={settings} onDraftChange={report} openGlobal={openGlobal} onConnectionsChanged={changed} />;
}
