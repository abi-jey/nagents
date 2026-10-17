import { useEffect, useState } from "react";
import { request } from "../../../api/client.js";
import { ToolsDialog } from "../../tools/ToolsDialog.js";
import { InstructionField } from "./InstructionField.js";
import { equal, inheritField, readJson, sourceLabel, targetQuery, useNavigatorDraft } from "./state.js";
import type { AgentPreferences, AgentSettingsReply, NavigatorSection, NavigatorTarget } from "./state.js";

export function PreferenceEditor({ token, target, section, report, snapshotChanged }: {
  token: string; target: NavigatorTarget; section: NavigatorSection;
  report: (section: NavigatorSection, dirty: boolean) => void; snapshotChanged: (reply: AgentSettingsReply) => void;
}) {
  const path = `agent-settings?${targetQuery(target)}`;
  const editor = useNavigatorDraft<AgentSettingsReply, AgentPreferences>({
    read: signal => readJson(path, token, signal), create: reply => ({ ...reply.preferences }),
    write: async (snapshot, preferences) => (await request("agent-settings", token, { scope: target.scope, ...(target.scope === "agent" ? { agent: target.agent } : {}), revision: snapshot.revision, preferences }, undefined, "PUT")).json() as Promise<AgentSettingsReply>,
  });
  const [advanced, setAdvanced] = useState(false);
  const [policyVisited, setPolicyVisited] = useState(false);
  const [policyDirty, setPolicyDirty] = useState(false);
  const [search, setSearch] = useState("");
  const { snapshot, draft, pending, error, recovery } = editor;
  const instructionsDirty = !!snapshot && !!draft && !equal(snapshot.preferences.instructions, draft.instructions);
  const toolsDirty = !!snapshot && !!draft && !equal(snapshot.preferences.tools, draft.tools);
  useEffect(() => { report("Instructions", instructionsDirty); report("Tools", toolsDirty || policyDirty); }, [instructionsDirty, toolsDirty, policyDirty, report]);
  useEffect(() => { if (snapshot) snapshotChanged(snapshot); }, [snapshot, snapshotChanged]);
  const field = section === "Tools" ? "tools" : "instructions";
  const inherited = snapshot?.inherited || { instructions: snapshot?.definition.instructions || "", tools: snapshot?.definition.tools || snapshot?.values.tools || [] };
  const instructions = draft?.instructions ?? inherited.instructions;
  const tools = draft?.tools ?? inherited.tools;
  const dirty = field === "instructions" ? instructionsDirty : toolsDirty;
  const defined = !!snapshot && target.scope === "agent" && (field === "instructions" ? !!snapshot.definition.instructions : snapshot.definition.tools !== null);
  const resetLabel = defined ? "Restore definition" : target.scope === "agent" ? "Use workspace default" : target.scope === "workspace" ? "Use global default" : "Use built-in default";
  function editField<K extends keyof AgentPreferences>(key: K, value: AgentPreferences[K]) {
    if (draft) editor.edit({ ...draft, [key]: value });
  }
  function savedField() {
    if (!snapshot || !draft) return;
    const posted = { ...snapshot.preferences };
    if (Object.hasOwn(draft, field)) Object.assign(posted, { [field]: draft[field] }); else delete posted[field];
    const other = field === "instructions" ? "tools" : "instructions";
    void editor.save(posted, (saved, before) => {
      if (Object.hasOwn(before, other)) Object.assign(saved, { [other]: before[other] }); else delete saved[other];
      return saved;
    });
  }
  function discardField() {
    if (!draft || !snapshot) return;
    const next = { ...draft };
    if (Object.hasOwn(snapshot.preferences, field)) Object.assign(next, { [field]: snapshot.preferences[field] }); else delete next[field];
    editor.edit(next);
  }
  return <div className="an-preferences">
    {pending && !snapshot && <p role="status">Loading agent preferences…</p>}
    {error && <p role="alert" className="error-text">{error}</p>}
    {(recovery || (!snapshot && error)) && <div className="an-recovery"><p>Your draft is kept. Reload the saved preferences before saving again.</p><button disabled={pending} type="button" onClick={() => void editor.reload()}>{editor.dirty ? "Discard instruction and tool drafts and reload" : "Reload saved preferences"}</button></div>}
    {snapshot && draft && <>
      <div hidden={section !== "Instructions"}>
        <div className="an-section-heading"><div><h3>Instructions</h3><p>Additional guidance for agents using these defaults.</p></div></div>
        <InstructionField label="Additional instructions" value={instructions} custom={Object.hasOwn(draft, "instructions")} source={sourceLabel(snapshot.inherited_origins?.instructions || snapshot.origins.instructions)} inheritLabel={snapshot.definition.instructions && target.scope === "agent" ? "Use agent definition" : target.scope === "agent" ? "Inherit workspace" : target.scope === "workspace" ? "Inherit global" : "Use built-in default"} scopeLabel={target.scope === "agent" ? target.agent : target.scope} disabled={pending || recovery} dirty={instructionsDirty} storagePath={snapshot.preferences_path} sourcePath={(snapshot.inherited_origins?.instructions || snapshot.origins.instructions) === "definition" ? snapshot.definition_path : undefined} description="Additional guidance is saved as Markdown; built-in and workspace instructions still apply." customize={() => editField("instructions", instructions)} inherit={() => editor.edit(inheritField(draft, "instructions"))} change={text => editField("instructions", text)} />
        <details className="an-source"><summary>Source instructions · read-only</summary>
          {snapshot.definition_path && <code>{snapshot.definition_path}</code>}
          <h4>Agent definition</h4><pre>{snapshot.definition.instructions || "No additional instructions in the agent definition."}</pre>
          {snapshot.native_instructions && <><h4>Built-in instructions</h4><pre>{snapshot.native_instructions}</pre></>}
          {snapshot.workspace_instructions?.map(source => <div key={source.path}><h4>{source.path}</h4><pre>{source.text}</pre></div>)}
          <p>Native agent and workspace instructions remain separate from the additional preference above.</p>
        </details>
      </div>
      <div hidden={section !== "Tools"}>
        <div className="an-section-heading"><div><h3>Tools</h3><p>Select the capabilities this agent can request.</p></div></div>
        <p className="an-field-origin">{Object.hasOwn(draft, "tools") ? `${target.scope === "agent" ? "Agent" : target.scope === "global" ? "Global" : "Workspace"} preference${toolsDirty ? " · unsaved" : ""}` : toolsDirty ? "Will use inherited selection" : sourceLabel(snapshot.origins.tools)}<button type="button" disabled={pending || recovery || !Object.hasOwn(draft, "tools")} onClick={() => editor.edit(inheritField(draft, "tools"))}>{snapshot.definition.tools !== null && target.scope === "agent" ? "Restore definition" : resetLabel}</button></p>
        <label className="an-field"><span>Search tools</span><input type="search" value={search} onChange={event => setSearch(event.target.value)} placeholder="Name or description" /></label>
        <div className="an-tool-actions"><span>{tools.length} selected · {snapshot.tools.filter(tool => tools.includes(tool.name) && tool.available).length} available with this selection</span><button type="button" disabled={pending || recovery} onClick={() => editField("tools", [])}>Clear selection</button></div>
        {snapshot.tools.filter(tool => `${tool.name} ${tool.description}`.toLowerCase().includes(search.toLowerCase())).map(tool => <article className="an-tool" key={tool.name}>
          <label><span><strong>{tool.name}</strong><small>{tool.description}</small>{!tool.available && <small className="an-tool-blocked">{tool.reason || "Unavailable for this agent."}</small>}</span><input type="checkbox" aria-label={`Allow ${tool.name}`} checked={tools.includes(tool.name)} disabled={pending || recovery || (!tool.available && !tools.includes(tool.name))} onChange={event => editField("tools", event.target.checked ? [...tools, tool.name] : tools.filter(name => name !== tool.name))} /></label>
          <details><summary>Tool schema · read-only</summary><pre>{JSON.stringify(tool.parameters, null, 2)}</pre></details>
        </article>)}
        {!snapshot.tools.length && <p>No tools are registered in this workspace.</p>}
        <p className="an-footnote">Tool selection stays within the agent definition, read-only restrictions, and workspace tool policy. Approval is still required where configured.</p>
      </div>
      <footer className="an-save-bar"><span>{dirty ? `Unsaved ${section.toLowerCase()} · ${target.scope === "agent" ? target.agent : target.scope}` : editor.notice || `Saved ${section.toLowerCase()}`}</span><button type="button" disabled={pending || !dirty} onClick={discardField}>Discard {section.toLowerCase()}</button><button className="primary" type="button" disabled={pending || recovery || !dirty} onClick={savedField}>{pending ? "Saving…" : `Save ${section.toLowerCase()}`}</button></footer>
    </>}
    <div hidden={section !== "Tools"} className="an-tool-management">
      <button type="button" aria-expanded={advanced} onClick={() => { setPolicyVisited(true); setAdvanced(value => !value); }}>Workspace tool policy, MCP &amp; approvals <span aria-hidden="true">{advanced ? "−" : "+"}</span>{policyDirty && " · unsaved"}</button>
      <div hidden={!advanced}>{policyVisited && <ToolsDialog embedded token={token} blocked={false} close={() => setAdvanced(false)} onDraftChange={setPolicyDirty} />}</div>
    </div>
  </div>;
}
