import { useEffect, useState } from "react";
import { readDashboard, type DashboardSnapshot } from "./dashboard.js";

type Scope = "workspace" | "global";
type Connection = { state: "loading" } | { state: "ready"; snapshot: DashboardSnapshot } | { state: "error" };

const metrics = [
  { label: "Usage", detail: "Requests and activity" },
  { label: "Input tokens", detail: "Prompt consumption" },
  { label: "Output tokens", detail: "Generated responses" },
  { label: "Total tokens", detail: "Combined consumption" },
];
const sections = [
  { id: "skills", title: "Skills", description: "Reusable instructions and capabilities.", empty: "Skill inventory is not connected." },
  { id: "agents", title: "Custom agents", description: "Specialized agents and their configuration.", empty: "Agent inventory is not connected." },
  { id: "models", title: "Models", description: "Provider connections and model selection.", empty: "Model inventory is not connected." },
  { id: "workspaces", title: "Workspaces", description: "Workspace inventory and scope.", empty: "Workspace discovery is not connected." },
];

export function Dashboard() {
  const [scope, setScope] = useState<Scope>("workspace");
  const [connection, setConnection] = useState<Connection>({ state: "loading" });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    document.title = "Dashboard | ngn";
    const controller = new AbortController();
    setConnection({ state: "loading" });
    void readDashboard(controller.signal).then(snapshot => {
      if (!controller.signal.aborted) setConnection({ state: "ready", snapshot });
    }).catch(() => {
      if (!controller.signal.aborted) setConnection({ state: "error" });
    });
    return () => controller.abort();
  }, [attempt]);
  const workspace = connection.state === "ready" ? connection.snapshot.workspace : "";
  return <main className="dashboard-shell">
    <div className="dashboard-content">
      <header className="dashboard-topbar">
        <a href="/" className="dashboard-brand" aria-label="ngn chat">ngn<span> / dashboard</span></a>
        <a href="/" className="dashboard-back">Back to chat <span aria-hidden="true">&rarr;</span></a>
      </header>
      <section className="dashboard-intro" aria-labelledby="dashboard-title">
        <p className="dashboard-eyebrow">Your agent environment</p>
        <div className="dashboard-heading"><h1 id="dashboard-title">Overview</h1><span className="dashboard-badge">Skeleton preview</span></div>
        <p>A place for usage, capabilities, and the workspaces that connect them.</p>
      </section>
      <div className="dashboard-toolbar">
        <div className="dashboard-scope" role="group" aria-label="Dashboard scope">
          <button type="button" aria-pressed={scope === "workspace"} onClick={() => setScope("workspace")}>Workspace</button>
          <button type="button" aria-pressed={scope === "global"} onClick={() => setScope("global")}>Global</button>
        </div>
        <p className="dashboard-scope-label" aria-live="polite">{scope === "global" ? "All workspaces (preview)" : workspace || "Current workspace"}</p>
      </div>
      <aside className="dashboard-notice" aria-label="Instrumentation status">
        <span className="dashboard-notice-mark" aria-hidden="true">i</span>
        <div><strong>Instrumentation not connected</strong><p>This is a frontend skeleton. Usage is not being measured here, and resource inventories are not loaded. A dash means unavailable, not zero.</p></div>
      </aside>
      <div className="dashboard-connection" role="status">
        {connection.state === "loading" && "Loading local workspace metadata..."}
        {connection.state === "ready" && "Local workspace identified. Analytics and inventories remain disconnected."}
        {connection.state === "error" && <>Could not load local workspace metadata. <button type="button" onClick={() => setAttempt(value => value + 1)}>Retry connection</button></>}
      </div>
      <section aria-labelledby="dashboard-usage-title" className="dashboard-usage">
        <div className="dashboard-section-heading"><h2 id="dashboard-usage-title">Usage &amp; tokens</h2><span>No measurements available</span></div>
        <dl className="dashboard-metrics">{metrics.map(metric => <div className="dashboard-metric" key={metric.label}>
          <dt>{metric.label}</dt><dd><span aria-hidden="true">&mdash;</span><span className="dashboard-sr-only">Unavailable</span></dd><p>{metric.detail}</p>
        </div>)}</dl>
      </section>
      <section className="dashboard-resources" aria-label={`${scope === "workspace" ? "Workspace" : "Global"} resources`}>
        {sections.map(section => <article className="dashboard-resource" key={section.id} aria-labelledby={`dashboard-${section.id}`}>
          <header><h2 id={`dashboard-${section.id}`}>{section.title}</h2><span>Not connected</span></header>
          <p>{section.description}</p>
          <div className="dashboard-empty"><span className="dashboard-empty-mark" aria-hidden="true">&mdash;</span><strong>{section.empty}</strong><p>{scope === "workspace" ? "Current workspace" : "Global"} data will appear here when connected.</p></div>
          {section.id === "workspaces" && workspace && <p className="dashboard-workspace-context">Current server workspace <code>{workspace}</code></p>}
        </article>)}
      </section>
      <footer className="dashboard-footer"><span>Local dashboard</span><span>Scope controls are preview-only. No configuration is changed.</span></footer>
    </div>
  </main>;
}
