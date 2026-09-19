import { useState } from "react";
import type { Design } from "./types";

export function Resources({ design, change }: { design: Design; change: (value: Design) => void }) {
  const [name, setName] = useState("");
  const valid = /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(name);
  const providers = Object.keys(design.providers), secrets = Object.keys(design.secrets);
  return <section aria-label="Shared resources"><h2>Shared resources</h2>
    <label>Default provider<select value={design.defaults.provider} onChange={(e) => change({ ...design, defaults: { ...design.defaults, provider: e.target.value } })}>{providers.map((id) => <option key={id}>{id}</option>)}</select></label>
    <label>Maximum delegation depth<input type="number" min="0" max="8" value={design.defaults.max_subagent_depth} onChange={(e) => change({ ...design, defaults: { ...design.defaults, max_subagent_depth: Number(e.target.value) } })} /></label>
    <label>New resource ID<input value={name} onChange={(e) => setName(e.target.value)} /></label>
    <div className="designer-tabs">
      <button disabled={!valid || !!design.providers[name]} onClick={() => { change({ ...design, providers: { ...design.providers, [name]: { type: "openai", model: "gpt-4.1", base_url: "", api: "auto", api_version: "", secret: secrets[0] || "" } } }); setName(""); }}>Add provider</button>
      <button disabled={!valid || !!design.secrets[name]} onClick={() => { change({ ...design, secrets: { ...design.secrets, [name]: { source: "env", name: "OPENAI_API_KEY" } } }); setName(""); }}>Add secret reference</button>
      <button disabled={!valid || !!design.mcp_servers[name]} onClick={() => { change({ ...design, mcp_servers: { ...design.mcp_servers, [name]: { transport: "stdio", command: "", args: [], env: {}, secrets: {} } } }); setName(""); }}>Add MCP server</button>
    </div>
    {Object.entries(design.providers).map(([id, provider]) => {
      const update = (patch: Partial<typeof provider>) => change({ ...design, providers: { ...design.providers, [id]: { ...provider, ...patch } } });
      return <details key={id}><summary>Provider · {id} · {provider.model}</summary>
        <label>Provider type<select value={provider.type} onChange={(e) => update({ type: e.target.value })}>{["openai", "openai_compatible", "anthropic", "gemini", "gemini_native", "openrouter", "litellm", "azure_openai_compatible", "azure_openai_compatible_v1"].map((kind) => <option key={kind}>{kind}</option>)}</select></label>
        <label>Model<input value={provider.model} onChange={(e) => update({ model: e.target.value })} /></label>
        <label>Authentication<select value={provider.auth || "api-key"} onChange={(e) => update({ auth: e.target.value as "api-key" | "chatgpt" })}><option value="api-key">API key reference</option><option value="chatgpt">Saved ChatGPT login</option></select></label>
        <label>API prefix URL<input value={provider.base_url} placeholder="Provider default" onChange={(e) => update({ base_url: e.target.value })} /></label>
        <label>API contract<select value={provider.api} onChange={(e) => update({ api: e.target.value })}>{["auto", "chat_completions", "responses", "messages"].map((api) => <option key={api}>{api}</option>)}</select></label>
        <label>Azure API version<input value={provider.api_version} onChange={(e) => update({ api_version: e.target.value })} /></label>
        {provider.auth !== "chatgpt" && <label>Credential reference<select value={provider.secret} onChange={(e) => update({ secret: e.target.value })}><option value="">Select reference…</option>{secrets.map((ref) => <option key={ref}>{ref}</option>)}</select></label>}
        <button disabled={design.defaults.provider === id || Object.values(design.agents).some((agent) => agent.provider === id)} onClick={() => { const next = { ...design.providers }; delete next[id]; change({ ...design, providers: next }); }}>Remove unused provider</button>
      </details>;
    })}
    {Object.entries(design.secrets).map(([id, secret]) => <details key={id}><summary>Secret reference · {id}</summary>
      <label>Source<select value={secret.source} onChange={(e) => change({ ...design, secrets: { ...design.secrets, [id]: { ...secret, source: e.target.value as "env" | "saved" } } })}><option value="env">Environment variable</option><option value="saved">Saved ngn provider credential</option></select></label>
      <label>{secret.source === "env" ? "Environment variable name" : "Saved provider name"}<input value={secret.name} onChange={(e) => change({ ...design, secrets: { ...design.secrets, [id]: { ...secret, name: e.target.value } } })} /></label>
      <p>Resolved on the server at execution time. Enter a reference name, not the secret value.</p>
    </details>)}
    {Object.entries(design.mcp_servers).map(([id, server]) => <details key={id}><summary>MCP server · {id} · stdio</summary>
      <label>Executable<input value={server.command} onChange={(e) => change({ ...design, mcp_servers: { ...design.mcp_servers, [id]: { ...server, command: e.target.value } } })} /></label>
      <label>Arguments (one per line)<textarea value={server.args.join("\n")} onChange={(e) => change({ ...design, mcp_servers: { ...design.mcp_servers, [id]: { ...server, args: e.target.value ? e.target.value.split("\n") : [] } } })} /></label>
      <p>Use YAML or Advanced resource JSON to bind environment variables and secret references.</p>
    </details>)}
  </section>;
}
