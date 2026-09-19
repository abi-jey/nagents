import type { Design } from "./types";

export function ProviderSettings({ provider, secrets, update }: {
  provider: Design["providers"][string]; secrets: string[];
  update: (patch: Partial<Design["providers"][string]>) => void;
}) {
  const chatgpt = provider.auth === "chatgpt";
  return <>
    <label>Provider type<select value={provider.type} onChange={(e) => update({ type: e.target.value })}>{["openai", "openai_compatible", "anthropic", "gemini", "gemini_native", "openrouter", "litellm", "azure", "azure_openai_compatible", "azure_openai_compatible_v1"].map((kind) => <option key={kind}>{kind}</option>)}</select></label>
    <label>Model ID<input value={provider.model} onChange={(e) => update({ model: e.target.value })} /></label>
    <label>API contract<select value={provider.api} disabled={chatgpt} onChange={(e) => update({ api: e.target.value })}>
      <option value="auto">Auto (provider default)</option><option value="responses">Responses API</option><option value="chat_completions">Chat Completions API</option><option value="messages">Messages API (Anthropic)</option>
    </select></label>
    <p>API selection chooses the request format, not the model. The endpoint must support the selected contract.</p>
    <label>Authentication<select value={provider.auth || "api-key"} onChange={(e) => update(e.target.value === "chatgpt" ? { auth: "chatgpt", type: "openai", base_url: "", api: "auto", secret: "" } : { auth: "api-key" })}><option value="api-key">API key reference</option><option value="chatgpt">Saved ChatGPT login</option></select></label>
    <label>API prefix URL<input value={provider.base_url} disabled={chatgpt} placeholder="Provider default" onChange={(e) => update({ base_url: e.target.value })} /></label>
    <label>Azure API version<input value={provider.api_version} placeholder="Provider default" onChange={(e) => update({ api_version: e.target.value })} /></label>
    {!chatgpt && <label>Credential reference<select value={provider.secret} onChange={(e) => update({ secret: e.target.value })}><option value="">Select reference…</option>{secrets.map((ref) => <option key={ref}>{ref}</option>)}</select></label>}
    {chatgpt && <p>Saved ChatGPT login uses the default OpenAI connection with automatic API routing.</p>}
  </>;
}
