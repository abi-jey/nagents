import { Icon } from "../components/Icon";
import { ContextIndicator } from "../features/context/ContextIndicator";
import type { Client } from "./useClient";

export function WorkspaceHeader({ client, navOpen, toggleNavigation }: {
  client: Client; navOpen: boolean; toggleNavigation: () => void;
}) {
  const { sessions, context } = client;
  const title = sessions.sessions.find((session) => session.id === sessions.sessionId)?.title || "New session";
  const model = sessions.config ? `${sessions.config.agent}: ${sessions.config.model}` : "Connecting…";
  return (
    <header className="topbar">
      <button className="nav-toggle" aria-label="Toggle sessions" aria-expanded={navOpen}
        aria-controls="session-navigation" title="Sessions" onClick={toggleNavigation}><Icon name="menu" /></button>
      <div className="model">
        <h1 title={title}>{title}</h1>
        <span title={model}>{model}</span>
      </div>
      <button type="button" id="live-launch" className={`live-launch${client.liveOpen ? " is-open" : ""}`} aria-label="Open voice controls" aria-expanded={client.liveOpen} aria-controls="live-voice-dock" title="Voice in this chat"
        disabled={!client.available.live} onClick={client.showLive}><Icon name="wave" size={16} /><span>GPT-Live</span></button>
      <ContextIndicator {...context} />
    </header>
  );
}
