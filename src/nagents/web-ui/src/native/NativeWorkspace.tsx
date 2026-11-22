import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { PanelDivider } from "../app/PanelDivider.js";
import { useAppViewport } from "../app/useAppViewport.js";
import { usePanelLayout } from "../app/usePanelLayout.js";
import { WorkspaceActivity } from "../app/WorkspaceActivity.js";
import { WorkspaceTopbar } from "../app/WorkspaceHeader.js";
import { Icon } from "../components/Icon.js";
import { ApprovalReview } from "../features/approvals/ApprovalReview.js";
import { Composer } from "../features/chat/Composer.js";
import { Conversation } from "../features/chat/Conversation.js";
import { SessionSidebar } from "../features/sessions/SessionSidebar.js";
import { useAppearance } from "../features/settings/appearance.js";
import { AppearanceSettings } from "../features/settings/AppearanceSettings.js";
import { AgentState, TeamIcon } from "../features/team/TeamTree.js";
import { activeRun, NativeClient, pendingApproval, steeringTarget } from "./client.js";
import { RunControls } from "./RunControls.js";
import { dialogApproval, isArchive, teamAgents } from "./transcript.js";

function AppearanceDialog({ close }: { close: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    element?.showModal();
    return () => { element?.close(); if (previous instanceof HTMLElement) previous.focus(); };
  }, []);
  return <dialog ref={dialog} className="workspace-dialog native-appearance" aria-labelledby="native-settings-title"
    onCancel={event => { event.preventDefault(); close(); }}>
    <header><h2 id="native-settings-title">Appearance</h2><button type="button" aria-label="Close settings" onClick={close}><Icon name="close" /></button></header>
    <AppearanceSettings />
  </dialog>;
}

export function NativeWorkspace() {
  const [client] = useState(() => new NativeClient());
  const view = useSyncExternalStore(client.subscribe, client.snapshot);
  useEffect(() => { void client.connect(); return () => client.dispose(); }, [client]);
  const shell = useRef<HTMLDivElement>(null), composer = useRef<HTMLTextAreaElement>(null);
  useAppViewport(shell);
  const [navOpen, setNavOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [teamOpen, setTeamOpen] = useState(() => window.matchMedia("(min-width: 1101px)").matches);
  const [settings, setSettings] = useState(false);
  const closeNavigation = useCallback(() => setNavOpen(false), []);
  const appearance = useAppearance();
  const panels = usePanelLayout(shell, sidebarCollapsed, teamOpen);
  const { config, session, child } = view;
  const run = activeRun(view);
  const approval = view.connected ? pendingApproval(view) : undefined;
  const agents = teamAgents(session, config, view.tasks, !!run, approval);
  const inspected = agents.find(agent => agent.id === child?.task.session_id);
  const sessions = view.sessions.map(item => ({
    id: item.id, title: item.title, updated_at: new Date(item.updated_at_ms).toISOString(),
    active_run_id: view.runs.find(run => run.session_id === item.id)?.id,
  }));
  const archive = isArchive(session);
  const archiveLabel = session.startsWith("host-archive-") ? "Read-only Rust host archive" : "Read-only Python archive";
  const model = archive ? archiveLabel : config?.model ? `Assistant: ${config.model}` : "Assistant";
  const submitting = view.bindings.some(binding => binding.session === session && binding.state === "sending");
  const canSubmit = view.connected && !!view.queue && !archive && !child;
  const steering = steeringTarget(view);
  const connection = view.connected ? "Connected" : view.reconnecting ? "Reconnecting…" :
    view.error ? "Disconnected" : "Connecting…";
  const status = approval ? "Waiting for approval" : run ? "Working…" : "Ready";
  const demo = config?.backend === "demo";

  function focusComposer() { requestAnimationFrame(() => composer.current?.focus({ preventScroll: true })); }
  async function select(id?: string) {
    if (id) client.select(id);
    else if (!(await client.create())) return;
    setNavOpen(false);
    focusComposer();
  }
  function submit(text?: string) {
    composer.current?.focus({ preventScroll: true });
    void client.send(text);
  }

  return <div ref={shell} className={`app-shell panel-layout-shell${sidebarCollapsed ? " rail-collapsed" : ""}`}
    style={panels.style} data-resizing={panels.dragging}>
    <a className="skip-link" href="#composer">Skip to prompt</a>
    <main className={["panel-layout", sidebarCollapsed && "sidebar-collapsed", teamOpen && "team-open"].filter(Boolean).join(" ")}>
      <WorkspaceTopbar title={sessions.find(item => item.id === session)?.title || "New session"}
        navOpen={navOpen} toggleNavigation={() => setNavOpen(value => !value)} teamOpen={teamOpen} toggleTeam={() => setTeamOpen(value => !value)}
        agent={<span title={model}>{model}</span>} status={<span className="native-connection mode-badge" role="status" data-connected={view.connected}>{connection}</span>} />
      <SessionSidebar workspace={config?.workspace || ""} sessions={sessions} selected={session}
        connection={config && { agent: "Assistant", provider: config.provider, model: config.model || "", connected: view.connected,
          readOnly: config.read_only, version: config.version }}
        disabled={!config || view.connecting || view.creating} newDisabled={!view.connected && !!session} open={navOpen} close={closeNavigation}
        select={id => void select(id)} settings={() => setSettings(true)} settingsDisabled={false} demo={demo} />
      <PanelDivider side="sessions" collapsed={sidebarCollapsed} setCollapsed={setSidebarCollapsed} layout={panels} />
      <WorkspaceActivity sessionId={session}
        conversation={{ entries: view.entries, sessionId: session, appearance, active: !child, agentName: "Assistant",
          demo: demo && !archive, canSubmit, submit,
          providerSetup: config && !config.model && !demo && !archive ? {
            configured: false, message: "No provider is configured. Set up an ngn provider on the host before sending a message.",
          } : undefined }}
        task={child && <section className="focused-task-conversation" aria-label={`${child.task.title} conversation`}>
          <header className="focused-task-header"><button type="button" className="task-back" onClick={() => { client.inspect(""); focusComposer(); }}>
            <Icon name="chevron" size={14} />Assistant</button>
            <div><h2>{child.task.title}</h2><span className="focused-task-model">Depth {child.task.depth} · {child.task.read_only ? "Read-only agent" : "Inspect-only conversation"}</span></div>
            {inspected && <AgentState agent={inspected} />}
          </header>
          <Conversation key={child.task.run_id} entries={child.entries} sessionId={child.task.session_id}
            appearance={appearance} demo={false} canSubmit={false} submit={() => {}} agentName={child.task.title} />
        </section>}
        composer={<footer className="composer-area">
          <div id="approval-waiting-slot" />
          {(view.error || view.reconnecting) && <div className="error-banner" role="alert">
            <span>{view.error || "Connection to ngn lost."}</span>
            <div className="feedback-actions">
              <button type="button" disabled={view.connecting} onClick={() => void client.connect()}>Reconnect</button>
              {view.error && <button type="button" aria-label="Dismiss error" onClick={() => client.dismiss()}>Dismiss</button>}
            </div>
          </div>}
          {view.warning && <p className="activity-warning" role="status">{view.warning}</p>}
          {child ? <div className="activity-banner">
            <span>Delegated conversations are inspect-only.</span>
            {child.task.status === "running" && <button type="button" disabled={!!view.stopping || !view.connected}
              onClick={() => void client.cancel(child.task.run_id)}>Stop this agent</button>}
          </div> : archive ? <p className="activity-warning">{archiveLabel}. Start a new session to send messages.</p> : <>
            <RunControls client={client} view={view} />
            <Composer inputRef={composer} prompt={view.draft} setPrompt={value => client.setDraft(value)} demo={demo}
              disabled={!config || !session} commandsEnabled={false} deliveryEnabled={false} canSubmit={canSubmit}
              running={!!run} submitting={submitting} stopping={!!run && view.stopping === run.id} submit={() => submit()}
              cancel={() => { if (run) void client.cancel(run.id); }}
              sendLabel={run || view.queue?.paused || view.queue?.entries.length ? "Queue" : "Send"}
              alternateAction={steering && <button type="button" title="Apply at the next persisted safe point"
                onMouseDown={event => event.preventDefault()} disabled={submitting || !view.draft.trim()}
                onClick={() => void client.send(undefined, true)}>Steer active turn</button>}
              status={<span className={`run-status${status === "Ready" ? " sr-only" : ""}`} role="status">{status}</span>} />
          </>}
        </footer>}
        team={{ team: { profiles: [], selected: child?.task.session_id || "", select: id => client.inspect(id),
          error: "", refresh: () => void client.connect(), subagentUsage: undefined },
          agents, arrivals: {}, open: teamOpen, close: () => setTeamOpen(false),
          limits: view.limits && `Per run: ${view.limits.max_concurrent} concurrent · ${view.limits.max_executions} executions · depth ${view.limits.max_depth}`,
        }} />
      <PanelDivider side="team" collapsed={!teamOpen} setCollapsed={value => setTeamOpen(!value)} layout={panels} />
      {!teamOpen && <aside className="team-collapsed-rail" id="team-navigation-collapsed" aria-label="Team conversations">
        <button type="button" aria-label="Expand team panel" title="Expand team panel" onClick={() => setTeamOpen(true)}><TeamIcon size={18} /></button>
        <span>{agents.length}</span>
      </aside>}
    </main>
    {settings && <AppearanceDialog close={() => setSettings(false)} />}
    <ApprovalReview sessionId={session} pending={dialogApproval(approval)} busy={!!view.deciding || !view.connected}
      error={view.error} decide={decision => { if (approval && decision !== "allow_tool") void client.decide(approval, decision === "allow"); }}
      cancel={() => { if (approval) void client.cancel(approval.run_id); }} />
  </div>;
}
