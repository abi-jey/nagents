import { useCallback, useEffect, useRef, useState } from "react";
import { useClient } from "./useClient";
import { WorkspaceHeader } from "./WorkspaceHeader";
import { WorkspaceSidebar } from "./WorkspaceSidebar";
import { WorkspaceComposer } from "./WorkspaceComposer";
import { WorkspaceDialogs } from "./WorkspaceDialogs";
import { taskAgent, conversationAgent } from "../features/team/model.js";
import { useAgentArrivals } from "../features/team/TeamTree.js";
import { AgentConfiguration } from "../features/team/TeamPane.js";
import { TaskTimeline } from "../features/team/TaskTimeline.js";
import { TaskConversation } from "../features/team/TaskConversation.js";
import { WorkspaceActivity } from "./WorkspaceActivity.js";
import "../features/team/team.css";

export function App() {
  const client = useClient();
  const [navOpen, setNavOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [teamOpen, setTeamOpen] = useState(() => typeof matchMedia !== "undefined" && matchMedia("(min-width: 1101px)").matches);
  const [configuredAgent, setConfiguredAgent] = useState("");
  const closeNavigation = useCallback(() => setNavOpen(false), []);
  const composer = useRef<HTMLTextAreaElement>(null);
  const root = conversationAgent(client.sessionAgent.selection, client.context.stats, !!client.chat.runId, !!client.chat.approval.pending);
  const agents = [root, ...client.team.tasks.map(task => {
    const agent = taskAgent(task);
    return client.chat.approval.pending?.task_id === task.id ? { ...agent, phase: "waiting" as const, reason: "Waiting for approval" } : agent;
  })];
  const arrivals = useAgentArrivals(client.team.tasks, client.sessions.sessionId, client.team.loaded);
  const configured = agents.find(agent => agent.id === configuredAgent);
  function configure(id: string) { if (id) setConfiguredAgent(id); else client.sessionAgent.show(); }
  useEffect(() => {
    if (client.navigationRequest) { setNavOpen(true); setSidebarCollapsed(false); }
  }, [client.navigationRequest]);

  async function select(id?: string) {
    if (await client.select(id)) {
      setNavOpen(false);
      requestAnimationFrame(() => composer.current?.focus({ preventScroll: true }));
    }
  }
  async function submit(value?: string) {
    composer.current?.focus({ preventScroll: true });
    if (client.team.selected) await client.team.submit();
    else await client.submit(value);
  }
  function closePanel() {
    const fromDesigner = client.panel === "designer";
    client.closePanel();
    if (fromDesigner) requestAnimationFrame(() => composer.current?.focus({ preventScroll: true }));
  }

  return (
    <div className={`app-shell${sidebarCollapsed ? " rail-collapsed" : ""}`}>
      <a className="skip-link" href="#composer">Skip to prompt</a>
      <main inert={client.panel === "designer"} className={[sidebarCollapsed && "sidebar-collapsed", client.liveOpen && "voice-open", teamOpen && "team-open"].filter(Boolean).join(" ")}>
        <WorkspaceHeader client={client} navOpen={navOpen} toggleNavigation={() => setNavOpen(!navOpen)} teamOpen={teamOpen} toggleTeam={() => setTeamOpen(value => !value)} />
        <WorkspaceSidebar client={client} composer={composer} open={navOpen} close={closeNavigation}
          select={select} collapsed={sidebarCollapsed} toggleCollapsed={() => setSidebarCollapsed(!sidebarCollapsed)} />
        <WorkspaceActivity sessionId={client.sessions.sessionId}
          task={client.team.selected && <TaskConversation team={client.team} agents={agents} arrivals={arrivals} token={client.sessions.config?.token || ""} sessionId={client.sessions.sessionId} back={() => { client.team.select(""); requestAnimationFrame(() => composer.current?.focus({ preventScroll: true })); }} />}
          conversation={{ token: client.sessions.config?.token || "", active: !client.team.selected,
            entries: client.chat.entries, sessionId: client.sessions.sessionId, demo: !!client.sessions.config?.demo,
            providerSetup: client.sessions.config?.provider_setup, canSubmit: client.available.submit, submit: value => void submit(value),
            renderTask: thread => agents.some(agent => agent.id === thread.id) ? <TaskTimeline id={thread.id} agents={agents} inspect={client.team.select} arrivals={arrivals} /> : undefined }}
          composer={<WorkspaceComposer client={client} composer={composer} submit={() => void submit()} select={select} />}
          team={{ team: client.team, agents, arrivals, open: teamOpen, close: () => setTeamOpen(false), configure }} />
      </main>
      {configuredAgent && configured && <AgentConfiguration key={`${client.sessions.sessionId}:${configuredAgent}`} agent={configured} token={client.sessions.config?.token || ""} sessionId={client.sessions.sessionId} changed={client.team.refresh} close={() => setConfiguredAgent("")} />}
      <WorkspaceDialogs client={client} select={select} closePanel={closePanel} />
    </div>
  );
}
