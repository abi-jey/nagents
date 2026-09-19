import { useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { ProviderSettings } from "./ProviderSettings";
import { newAgent } from "./types";
import type { Design } from "./types";

export function NewDesignDialog({ template, names, create, close }: {
  template: Design; names: string[]; create: (design: Design) => Promise<void>; close: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [id, setId] = useState("");
  const [agentId, setAgentId] = useState("assistant");
  const [agent, setAgent] = useState({ ...newAgent(), name: "Assistant" });
  const [resources, setResources] = useState(() => ({ ...structuredClone(template), secrets: Object.keys(template.secrets).length ? structuredClone(template.secrets) : { primary_key: { source: "env" as const, name: "OPENAI_API_KEY" } } }));
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const providerId = resources.defaults.provider;
  const provider = resources.providers[providerId];
  const valid = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/;
  useEffect(() => {
    const previous = document.activeElement;
    dialog.current?.showModal();
    return () => { dialog.current?.close(); if (previous instanceof HTMLElement) previous.focus(); };
  }, []);
  async function submit(event: FormEvent) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true; setPending(true); setError("");
    try {
      await create({ ...resources, id, entrypoint: agentId, agents: { [agentId]: agent }, layout: {}, channels: {}, mcp_servers: {} });
    } catch (cause) { setError(cause instanceof Error ? cause.message : "Could not create design."); }
    finally { submitting.current = false; setPending(false); }
  }
  return <dialog ref={dialog} className="designer-new-dialog" aria-labelledby="new-design-title" onCancel={(event) => { event.preventDefault(); if (!pending) close(); }}>
    <form onSubmit={(event) => void submit(event)}>
      <header><h2 id="new-design-title">New design <small>Experimental</small></h2><p>Set up your first agent and provider. Add more agents, tools, MCPs, and channel bindings in the editor.</p></header>
      <fieldset disabled={pending}><legend>Design and first agent</legend>
        <label>Design ID<input autoFocus required pattern="[A-Za-z][A-Za-z0-9_-]{0,63}" placeholder="my-team" value={id} onChange={(e) => setId(e.target.value)} /></label>
        {names.includes(id) && <p role="alert">A design with this ID already exists.</p>}
        <label>Agent ID<input required pattern="[A-Za-z][A-Za-z0-9_-]{0,63}" value={agentId} onChange={(e) => setAgentId(e.target.value)} /></label>
        <label>Display name<input value={agent.name} onChange={(e) => setAgent({ ...agent, name: e.target.value })} /></label>
        <label>Instructions<textarea value={agent.instructions.text} onChange={(e) => setAgent({ ...agent, instructions: { ...agent.instructions, text: e.target.value } })} /></label>
      </fieldset>
      <fieldset disabled={pending}><legend>Provider and authentication</legend>
        <ProviderSettings provider={provider} secrets={Object.keys(resources.secrets)} update={(patch) => setResources({ ...resources, providers: { ...resources.providers, [providerId]: { ...provider, ...patch } } })} />
        {provider.auth !== "chatgpt" && Object.entries(resources.secrets).map(([name, secret]) => <div key={name}>
          <label>Credential source · {name}<select value={secret.source} onChange={(e) => setResources({ ...resources, secrets: { ...resources.secrets, [name]: { ...secret, source: e.target.value as "env" | "saved" } } })}><option value="env">Environment variable</option><option value="saved">Saved provider credential</option></select></label>
          <label>{secret.source === "env" ? "Environment variable name" : "Saved provider name"}<input required value={secret.name} onChange={(e) => setResources({ ...resources, secrets: { ...resources.secrets, [name]: { ...secret, name: e.target.value } } })} /></label>
        </div>)}
        <p>Use credential references, not secret values. Creating a design does not call a provider.</p>
      </fieldset>
      {error && <p role="alert" className="designer-error">{error}</p>}
      <footer><button type="button" disabled={pending} onClick={close}>Cancel</button><button type="submit" disabled={pending || !valid.test(id) || !valid.test(agentId) || names.includes(id)}>{pending ? "Creating…" : "Create design"}</button></footer>
    </form>
  </dialog>;
}
