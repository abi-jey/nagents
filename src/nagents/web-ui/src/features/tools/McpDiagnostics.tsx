export interface McpDiagnostics {
  status: "not_loaded" | "loaded" | "partial" | "retained" | "disabled";
  configured_servers: number;
  connected_servers: number;
  registered_tools: number;
  advertised_tools: number;
  effective_source: string;
  sources: { path: string; trusted: boolean; status: "read" | "missing" | "ignored" | "unavailable" | "not_read" }[];
  ignored_configs: string[];
  error: string;
  failed_servers?: {
    name: string;
    stage: "mcp_start" | "mcp_discover";
    category: string;
    message: string;
    retry_pending: boolean;
  }[];
}

const failureCategory: Record<string, string> = {
  missing_environment: "Missing environment variable",
  authorization: "Authorization needed",
  timeout: "Connection timed out",
  executable_not_found: "Executable not found",
  permission_denied: "Permission denied",
  disconnected: "Connection closed",
  invalid_configuration: "Invalid configuration",
  server_error: "Server error",
};

export function McpDiscovery({ value, retry, retrying = "", disabled = false, notice = "", error = "" }: {
  value?: McpDiagnostics;
  retry?: (server: string) => void;
  retrying?: string;
  disabled?: boolean;
  notice?: string;
  error?: string;
}) {
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
    {!!value.failed_servers?.length && <div className="tools-mcp-failures">
      <p role="status">Unavailable servers are skipped. Healthy tools remain available.</p>
      <ul aria-label="Unavailable MCP servers">{value.failed_servers.map(server => <li key={server.name} className="tools-mcp-failure">
        <div><strong>{server.name}</strong><span className="tools-mcp-failure-category">{failureCategory[server.category] || "Server unavailable"}</span>
          <p>{server.message}</p>
          <small>{server.retry_pending ? "Retry queued for the next safe response boundary or message." : "Skipped until you retry or its configuration changes."}</small>
        </div>
        {retry && <button type="button" disabled={disabled || !!retrying || server.retry_pending}
          aria-label={`Retry ${server.name}`} aria-busy={retrying === server.name}
          onClick={() => retry(server.name)}>{retrying === server.name ? "Retrying…" : server.retry_pending ? "Retry queued" : "Retry"}</button>}
      </li>)}</ul>
    </div>}
    {notice && <p role="status" className="tools-mcp-feedback">{notice}</p>}
    {error && <p role="alert" className="tools-mcp-feedback error-text">{error}</p>}
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
