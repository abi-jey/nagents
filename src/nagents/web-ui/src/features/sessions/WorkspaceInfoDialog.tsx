import { useEffect, useRef, useState } from "react";
import { request } from "../../api/client.js";
import { Icon } from "../../components/Icon.js";
import { ProviderLogo } from "../../components/ProviderLogo.js";
import type { McpDiagnostics } from "../tools/McpDiagnostics.js";

export type WorkspaceConnection = {
  token: string;
  agent: string;
  provider: string;
  model: string;
  connected: boolean;
};

interface ToolSnapshot {
  agents: Record<string, Record<string, boolean>>;
  profiles: { id: string; mode: string }[];
  tools: { name: string }[];
  mcp_diagnostics?: McpDiagnostics;
}

export function WorkspaceInfoDialog({ workspace, name, demo, count, running, connection, close, settings, tools,
  settingsDisabled, toolsDisabled }: {
  workspace: string; name: string; demo: boolean; count: number; running: number;
  connection?: WorkspaceConnection;
  close: () => void; settings: () => void; tools: () => void;
  settingsDisabled: boolean; toolsDisabled: boolean;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const [snapshot, setSnapshot] = useState<ToolSnapshot>();
  const [failed, setFailed] = useState(false);
  const [revision, setRevision] = useState(0);
  const token = connection?.token || "", agent = connection?.agent || "";
  useEffect(() => {
    const element = dialog.current, previous = document.activeElement;
    element?.showModal(); heading.current?.focus();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, []);
  useEffect(() => {
    setSnapshot(undefined); setFailed(false);
    if (!token) return;
    const controller = new AbortController();
    void (async () => {
      try {
        const next = await (await request("tools", token, undefined, controller.signal)).json() as ToolSnapshot;
        if (!controller.signal.aborted) setSnapshot(next);
      } catch {
        if (!controller.signal.aborted) setFailed(true);
      }
    })();
    return () => controller.abort();
  }, [token, agent, revision]);
  const selected = snapshot?.agents[agent] || {};
  const enabled = snapshot?.tools.filter(tool => selected[tool.name] !== false).length;
  const readOnly = snapshot?.profiles.some(profile => profile.id === agent && profile.mode === "reviewer");
  const mcp = snapshot?.mcp_diagnostics;
  function navigate(action: () => void) { close(); action(); }
  return <dialog ref={dialog} className="workspace-dialog workspace-overview" aria-labelledby="workspace-dialog-title"
    onCancel={event => { event.preventDefault(); close(); }}>
    <header><h2 id="workspace-dialog-title" ref={heading} tabIndex={-1}><Icon name="folder" />{name}</h2>
      <button type="button" onClick={close} aria-label="Close workspace information"><Icon name="close" /></button>
    </header>
    <p className="workspace-connection" role="status"><span className={connection?.connected ? "connected" : ""} aria-hidden="true" />
      {demo ? "Offline demo" : connection?.connected ? "Connected to ngn" : "Connecting to ngn…"}</p>
    <dl className="workspace-location"><dt>Workspace folder on the server</dt><dd><code>{workspace || "Connecting…"}</code></dd></dl>
    <section className="workspace-assistant" aria-labelledby="workspace-assistant-title">
      <h3 id="workspace-assistant-title">Current assistant</h3>
      <dl><div><dt>Agent</dt><dd>{agent || "Not connected"}{readOnly && <span className="workspace-readonly">Read-only</span>}</dd></div>
        <div><dt>Provider</dt><dd className="workspace-provider">{connection?.provider && <ProviderLogo provider={connection.provider} />}{connection?.provider || "Not connected"}</dd></div>
        <div><dt>Model</dt><dd>{connection?.model || "Not connected"}</dd></div></dl>
    </section>
    <section className="workspace-resources" aria-labelledby="workspace-resources-title">
      <h3 id="workspace-resources-title">Workspace activity</h3>
      <dl><div><dt>Chats</dt><dd><strong>{count}</strong><span>{running ? `${running} running` : "None running"}</span></dd></div>
        <div><dt>Tools enabled for {agent || "this agent"}</dt><dd>{snapshot ? <><strong>{enabled} <small>/ {snapshot.tools.length}</small></strong><span>{readOnly ? "Read-only restrictions apply" : "Registered tools"}</span></> :
          <span role="status">{failed ? "Could not load tools" : token ? "Loading…" : "Reconnect to load tools"}</span>}</dd></div></dl>
      {mcp && <p className="workspace-mcp">{mcp.connected_servers} of {mcp.configured_servers} MCP servers connected
        {mcp.status === "retained" ? " · Previous configuration; reload failed" : mcp.status === "disabled" ? " · Startup disabled" : mcp.status === "not_loaded" ? " · Discovery pending" : ""}
        {mcp.status !== "retained" && !!mcp.error && " · Connection error; see Tools"}</p>}
      {failed && <button type="button" className="workspace-retry" onClick={() => setRevision(value => value + 1)}>Retry tools</button>}
    </section>
    <footer>
      <button type="button" disabled={toolsDisabled} onClick={() => navigate(tools)}><Icon name="tools" size={16} />Manage tools</button>
      <button type="button" disabled={settingsDisabled} onClick={() => navigate(settings)}><Icon name="settings" size={16} />Settings</button>
    </footer>
  </dialog>;
}
