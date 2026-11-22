import { Icon } from "../components/Icon";
import { ContextIndicator } from "../features/context/ContextIndicator";
import type { Client } from "./useClient";
import { TeamIcon } from "../features/team/TeamTree.js";
import type { ReactNode } from "react";

type Navigation = { navOpen: boolean; toggleNavigation: () => void; teamOpen?: boolean; toggleTeam?: () => void };

export function WorkspaceTopbar({ title, agent, status, navOpen, toggleNavigation, teamOpen = false, toggleTeam }: Navigation & {
  title: string; agent?: ReactNode; status?: ReactNode;
}) {
  return (
    <header className="topbar">
      <button className="nav-toggle" aria-label="Toggle sessions" aria-expanded={navOpen}
        aria-controls="session-navigation" title="Sessions" onClick={toggleNavigation}><Icon name="menu" /></button>
      <div className="model">
        <h1 title={title}>{title}</h1>
        {agent}
      </div>
      {status}
      {toggleTeam && <button type="button" className="team-toggle" aria-label="Team conversations" aria-expanded={teamOpen} onClick={toggleTeam}><TeamIcon size={18} /><span>Team</span></button>}
    </header>
  );
}

export function WorkspaceHeader({ client, ...navigation }: Navigation & { client: Client }) {
  const { sessions, context } = client;
  const title = sessions.sessions.find((session) => session.id === sessions.sessionId)?.title || "New session";
  const agent = client.sessionAgent;
  const model = agent.identity ? `${agent.identity.agent}: ${agent.identity.model}` : "Loading agent…";
  return <WorkspaceTopbar {...navigation} title={title} status={<ContextIndicator {...context} />}
    agent={<button type="button" className="conversation-agent-trigger" onClick={agent.show}
      disabled={!client.available.settings} aria-label="Choose agent for this conversation" aria-haspopup="dialog"
      title={agent.selection?.pending ? `${model}. ${agent.selection.agent} will answer after this run.` : model}>
      <TeamIcon size={13} /><span>{model}</span>
      {agent.selection?.pending && <span className="conversation-agent-pending">Next: {agent.selection.agent}</span>}
      <Icon name="chevron" size={12} />
    </button>} />;
}
