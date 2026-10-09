export interface McpDiagnostics {
  status: "not_loaded" | "loaded" | "retained" | "disabled";
  configured_servers: number;
  connected_servers: number;
  registered_tools: number;
  advertised_tools: number;
  effective_source: string;
  sources: { path: string; trusted: boolean; status: "read" | "missing" | "ignored" | "unavailable" | "not_read" }[];
  ignored_configs: string[];
  error: string;
}

export function McpDiscovery({ value }: { value?: McpDiagnostics }) {
  if (!value) return null;
  const sourceStatus = { read: "Read", missing: "Missing", ignored: "Ignored", unavailable: "Unavailable", not_read: "Not read" };
  return <section className="tools-mcp" aria-labelledby="tools-mcp-title">
    <h3 id="tools-mcp-title">MCP discovery</h3>
    <p className="tools-mcp-count"><strong>{value.configured_servers} MCP {value.configured_servers === 1 ? "server" : "servers"} configured</strong>
      <span>{value.connected_servers} connected · {value.registered_tools} tools registered · {value.advertised_tools < 0 ? "Unknown" : value.advertised_tools} available to the current agent</span></p>
    {value.status === "retained" && <p role="status">The last reload failed. These counts and sources describe the previous working configuration.</p>}
    {value.status === "disabled" && <p>MCP startup is disabled for this harness. Configured servers were not started.</p>}
    {value.status === "not_loaded" && <p>MCP discovery has not completed yet.</p>}
    {!!value.error && <p className="error-text">{value.error}</p>}
    <details open={!value.configured_servers || value.status === "retained"}>
      <summary>Configuration sources</summary>
      <p>Effective MCP source: <code>{value.effective_source}</code></p>
      {value.sources.length ? <ul>{value.sources.map(source => <li key={source.path}>
        <code>{source.path}</code><span>{sourceStatus[source.status]} · {source.trusted ? "trusted" : "not trusted"}</span>
      </li>)}</ul> : <p>Programmatic configuration; this harness does not read MCP declarations from files.</p>}
      <p>Sources from the last successful configuration read. ngn reloads trusted settings before the next message and after each model response.</p>
    </details>
    {!!value.ignored_configs.length && <div className="tools-mcp-ignored" role="status"><strong>Other configuration files are ignored</strong>
      <ul>{value.ignored_configs.map(path => <li key={path}><code>{path}</code></li>)}</ul>
      <p>ngn does not import these files. Add approved <code>mcp_servers</code> declarations to a trusted ngn configuration.</p>
    </div>}
  </section>;
}
