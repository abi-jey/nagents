import { useCallback, useEffect, useRef, useState } from "react";
import { useClient } from "./useClient";
import { WorkspaceHeader } from "./WorkspaceHeader";
import { WorkspaceSidebar } from "./WorkspaceSidebar";
import { WorkspaceComposer } from "./WorkspaceComposer";
import { WorkspaceDialogs } from "./WorkspaceDialogs";
import { taskAgent, conversationAgent } from "../features/team/model.js";
import { useAgentArrivals, TeamIcon } from "../features/team/TeamTree.js";
import { AgentConfiguration } from "../features/team/TeamPane.js";
import { AgentNavigator } from "../features/settings/navigator/AgentNavigator.js";
import type { NavigatorAudio } from "../features/settings/navigator/AgentNavigator.js";
import type { SettingsReply } from "../features/settings/types.js";
import type { NavigatorRequest, NavigatorSection } from "../features/settings/navigator/state.js";
import { isProfileTarget, profileSelection, profileTarget } from "../features/team/useTeam.js";
import { TaskConversation } from "../features/team/TaskConversation.js";
import { WorkspaceActivity } from "./WorkspaceActivity.js";
import { useAppearance } from "../features/settings/appearance.js";
import { useAppViewport } from "./useAppViewport.js";
import { usePanelLayout } from "./usePanelLayout.js";
import { PanelDivider } from "./PanelDivider.js";
import "../features/team/team.css";
import "../features/team/teamGroups.css";
import "../features/chat/delegationActivity.css";
import "../features/settings/navigator/agentNavigator.css";

export function App() {
  const shell = useRef<HTMLDivElement>(null);
  useAppViewport(shell);
  const client = useClient();
  const appearance = useAppearance();
  const [navOpen, setNavOpen] = useState(false);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [teamOpen, setTeamOpen] = useState(() => typeof matchMedia !== "undefined" && matchMedia("(min-width: 1101px)").matches);
  const panels = usePanelLayout(shell, sidebarCollapsed, teamOpen);
  const [configuredAgent, setConfiguredAgent] = useState("");
  const [navigatorRequest, setNavigatorRequest] = useState<NavigatorRequest>();
  const [navigatorAudio, setNavigatorAudio] = useState<NavigatorAudio>();
  const navigatorSerial = useRef(0);
  const openAgentSettings = useCallback((agent: string, section: NavigatorSection = "Overview") => {
    setNavigatorRequest({ scope: "agent", agent, section, serial: ++navigatorSerial.current });
  }, []);
  const clientRef = useRef(client); clientRef.current = client;
  const acceptSettings = useCallback((reply: SettingsReply) => clientRef.current.sessions.acceptSettings(reply), []);
  const settingsChanged = useCallback(() => { clientRef.current.team.refresh(); void clientRef.current.sessionAgent.refresh(); void clientRef.current.settings.refresh(); }, []);
  useEffect(() => {
    if (!client.settings.open) return;
    setNavigatorRequest({ scope: client.settings.scope, agent: "", section: "Overview", serial: ++navigatorSerial.current });
    client.settings.close();
  }, [client.settings.open]);
  useEffect(() => {
    if (client.panel !== "tools") return;
    setNavigatorRequest({ scope: "workspace", agent: "", section: "Tools", serial: ++navigatorSerial.current });
    client.closePanel();
  }, [client.panel]);
  const closeNavigation = useCallback(() => setNavOpen(false), []);
  const composer = useRef<HTMLTextAreaElement>(null);
  const root = conversationAgent(client.sessionAgent.selection, client.context.stats, !!client.chat.runId, !!client.chat.approval.pending);
  const agents = [root, ...client.team.tasks.map(task => {
    const agent = taskAgent(task);
    return client.chat.approval.pending?.session_id === task.session_id ? { ...agent, phase: "waiting" as const, reason: "Waiting for approval" } : agent;
  })].map(agent => {
    const profile = client.team.profiles.find(profile => profile.id === agent.profile);
    return { ...agent, authentication: profile?.authentication, trustRemoteTools: profile?.trust_remote_tools };
  });
  const arrivals = useAgentArrivals(client.team.tasks, client.sessions.sessionId, client.team.loaded);
  const configured = agents.find(agent => agent.id === configuredAgent);
  function configure(id: string) {
    const target = id || profileTarget(root.profile || client.sessionAgent.selection?.agent || "assistant");
    if (isProfileTarget(target)) { setConfiguredAgent(""); openAgentSettings(profileSelection(target).profile); }
    else setConfiguredAgent(target);
  }
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

  return (
    <div ref={shell} className={`app-shell panel-layout-shell${sidebarCollapsed ? " rail-collapsed" : ""}`} style={panels.style} data-resizing={panels.dragging}>
      <a className="skip-link" href="#composer">Skip to prompt</a>
      <main className={["panel-layout", sidebarCollapsed && "sidebar-collapsed", client.liveOpen && "voice-open", teamOpen && "team-open"].filter(Boolean).join(" ")}>
        <WorkspaceHeader client={client} navOpen={navOpen} toggleNavigation={() => setNavOpen(!navOpen)} teamOpen={teamOpen} toggleTeam={() => setTeamOpen(value => !value)} />
        <WorkspaceSidebar client={client} composer={composer} open={navOpen} close={closeNavigation}
          select={select} />
        <PanelDivider side="sessions" collapsed={sidebarCollapsed} setCollapsed={setSidebarCollapsed} layout={panels} />
        <WorkspaceActivity sessionId={client.sessions.sessionId}
          task={client.team.selected && <TaskConversation appearance={appearance} team={client.team} agents={agents} arrivals={arrivals} activity={client.chat.taskActivity} token={client.sessions.config?.token || ""} sessionId={client.sessions.sessionId} configureConnection={name => openAgentSettings(name)} back={() => { client.team.select(""); requestAnimationFrame(() => composer.current?.focus({ preventScroll: true })); }} />}
          conversation={{ appearance, agentName: root.name, token: client.sessions.config?.token || "", active: !client.team.selected,
            entries: client.chat.entries, sessionId: client.sessions.sessionId, demo: !!client.sessions.config?.demo,
            providerSetup: root.providerKind === "a2a" ? undefined : client.sessions.config?.provider_setup, canSubmit: client.available.submit, submit: value => void submit(value),
            delegation: { agents, profiles: client.team.profiles, activity: client.chat.taskActivity, inspect: client.team.select } }}
          composer={<WorkspaceComposer client={client} composer={composer} submit={() => void submit()} select={select}
            configureSettings={() => openAgentSettings(client.sessionAgent.selection?.agent || "assistant", "Voice")} audioChanged={setNavigatorAudio} />}
          team={{ team: client.team, agents, arrivals, open: teamOpen, close: () => setTeamOpen(false), configure }} />
        <PanelDivider side="team" collapsed={!teamOpen} setCollapsed={value => setTeamOpen(!value)} layout={panels} />
        {!teamOpen && <aside className="team-collapsed-rail" id="team-navigation-collapsed" aria-label="Team conversations">
          <button type="button" aria-label="Expand team panel" title="Expand team panel" onClick={() => setTeamOpen(true)}><TeamIcon size={18} /></button>
          <span>{agents.length}</span>
        </aside>}
      </main>
      {configuredAgent && configured && <AgentConfiguration key={`${client.sessions.sessionId}:${configuredAgent}`} agent={configured} token={client.sessions.config?.token || ""} sessionId={client.sessions.sessionId} changed={client.team.refresh} close={() => setConfiguredAgent("")}
        configureDefinition={client.team.profiles.some(profile => profile.id === configured.profile) ? () => { setConfiguredAgent(""); openAgentSettings(configured.profile); } : undefined} />}
      {client.sessions.config && <AgentNavigator key={client.sessions.config.token} request={navigatorRequest} token={client.sessions.config.token} audio={navigatorAudio}
        close={() => setNavigatorRequest(undefined)} changed={settingsChanged} acceptSettings={acceptSettings}
        catalogRevision={JSON.stringify(client.team.profiles.map(profile => [profile.id, profile.name, profile.description, profile.remote_name, profile.remote_description, profile.metadata_status, profile.provider, profile.model]))}
        workspaceName={client.sessions.config.workspace.split(/[\\/]/).filter(Boolean).at(-1) || "Workspace"} />}
      <WorkspaceDialogs client={client} select={select} />
    </div>
  );
}
