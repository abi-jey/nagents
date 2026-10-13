import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { Icon } from "../../components/Icon.js";
import { ProviderLogo } from "../../components/ProviderLogo.js";
import { contextPercent, contextSummary, contextUsage, contextUsageLabel, contextWindowSource } from "../context/controller.js";
import { compactionMarker, compactionWarning } from "./context.js";
import { agentAncestors, orderedTeam, phaseLabel, providerLabel, type TeamAgent, type TeamProfile } from "./model.js";
import { profileTarget } from "./useTeam.js";
import { scrollWithin } from "../../components/scrollWithin.js";

export type ArrivalTimes = Readonly<Record<string, number>>;
export function useArrivalStyle(at = 0): CSSProperties {
  return useMemo(() => at ? { "--arrival-delay": `${Math.min(0, at - Date.now())}ms` } as CSSProperties : {}, [at]);
}

export function TeamIcon({ size = 18 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="10" cy="5.5" r="2.4" /><path d="M6 17v-1.8a4 4 0 0 1 8 0V17M4.2 5a2.2 2.2 0 0 0 0 4.4M15.8 5a2.2 2.2 0 0 1 0 4.4M2 16v-1.3a3 3 0 0 1 1.8-2.7M18 16v-1.3a3 3 0 0 0-1.8-2.7" /></svg>;
}

export function AgentState({ agent }: { agent: TeamAgent }) {
  return <span className={`team-state state-${agent.phase}`} title={agent.reason}><i aria-hidden="true" />{phaseLabel(agent)}</span>;
}

export function AgentRow({ agent, selected, inspect, settings, arrivalAt = 0 }: {
  agent: TeamAgent; selected: boolean; inspect: () => void; settings: () => void; arrivalAt?: number;
}) {
  const context = agent.context;
  const marker = compactionMarker(agent);
  const warning = compactionWarning(agent);
  const provider = providerLabel(agent);
  const motion = useArrivalStyle(arrivalAt);
  return <article className={`team-agent-row${selected ? " is-selected" : ""}${arrivalAt ? " is-arriving" : ""}`} style={motion} data-agent-id={agent.id || "main"}>
    <button type="button" className="team-agent-open" onClick={inspect} aria-current={selected ? "page" : undefined} aria-description={marker?.description}
      aria-label={`Open ${agent.name} conversation. ${phaseLabel(agent)}${provider ? `. ${provider} ${agent.model}` : ""}`}>
      <span className="team-agent-heading"><strong>{agent.name}</strong><AgentState agent={agent} /></span>
      <span className="team-agent-model">{provider && <ProviderLogo provider={agent.providerKind} />}<span>{[provider, agent.model].filter(Boolean).join(" · ") || "Configuration unavailable"}</span></span>
      <span className="team-agent-context" title={context ? `${contextUsageLabel(context)}: ${contextUsage(context).toLocaleString()} tokens; ${contextWindowSource(context)}${marker ? `; ${marker.description}` : ""}` : undefined}>
        {context ? contextSummary(context) : "Context unavailable"}
      </span>
      {context?.context_window ? <span className="team-agent-meter" aria-hidden="true"><i style={{ width: `${contextPercent(context)}%` }} />
        {marker && <b className="team-agent-threshold" style={{ left: `${marker.percent}%` }} title={marker.description} />}
      </span> : null}
      {warning && <span className="team-context-warning">{warning}</span>}
    </button>
    <button type="button" className="team-agent-settings" aria-label={`Settings for ${agent.name}`} onClick={settings}><Icon name="settings" size={14} /></button>
  </article>;
}

export function TeamTree({ agents, profiles, selected, inspect, configure, arrivals, error, refresh }: {
  agents: TeamAgent[]; profiles: TeamProfile[]; selected: string; inspect: (id: string) => void; configure: (id: string) => void;
  arrivals: ArrivalTimes; error: string; refresh: () => void;
}) {
  const { children } = useMemo(() => orderedTeam(agents), [agents]);
  const [expanded, setExpanded] = useState(new Map<string, boolean>());
  const [limits, setLimits] = useState(new Map<string, number>());
  const [profileLimit, setProfileLimit] = useState(3);
  const tree = useRef<HTMLDivElement>(null);
  useEffect(() => {
    // Only a changed recipient reveals ancestry. New activity never overrides a manual collapse.
    const ancestors = agentAncestors(selected, agents);
    setExpanded(current => { const next = new Map(current); for (const id of ancestors) next.set(id, true); return next; });
    setLimits(current => { const next = new Map(current); for (const id of [selected, ...ancestors]) {
      const parent = agents.find(agent => agent.id === id)?.parentId || "";
      const index = (children.get(parent) || []).findIndex(agent => agent.id === id);
      if (index >= 0) next.set(parent, Math.max(current.get(parent) || 6, index + 1));
    } return next; });
    requestAnimationFrame(() => {
      const row = [...(tree.current?.querySelectorAll<HTMLElement>("[data-agent-id]") || [])].find(element => element.dataset.agentId === (selected || "main"));
      if (row && tree.current) scrollWithin(tree.current, row);
    });
  }, [selected]);
  useEffect(() => {
    if (!tree.current || typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting && entry.target instanceof HTMLElement) {
        const parent = entry.target.dataset.moreParent;
        if (parent !== undefined) setLimits(current => new Map(current).set(parent, Math.min((children.get(parent) || []).length, (current.get(parent) || 6) + 6)));
        if (entry.target.dataset.moreProfiles !== undefined) setProfileLimit(current => Math.min(profiles.filter(profile => !profile.used).length, current + 6));
      }
    }, { root: tree.current, rootMargin: "0px 0px 48px 0px" });
    for (const sentinel of tree.current.querySelectorAll("[data-more-parent], [data-more-profiles]")) observer.observe(sentinel);
    return () => observer.disconnect();
  }, [children, limits, expanded, profileLimit, profiles]);
  function branch(agent: TeamAgent, depth: number) {
    const descendants = children.get(agent.id) || [], open = expanded.get(agent.id) ?? depth < 2;
    const limit = limits.get(agent.id) || 6;
    return <li key={agent.id || "main"} className="team-tree-branch">
      <div className="team-tree-row"><span className="team-tree-toggle-slot">{!!descendants.length && <button type="button" className="team-tree-toggle" aria-label={`${open ? "Collapse" : "Expand"} ${agent.name}'s agents`} aria-expanded={open}
        onClick={() => setExpanded(current => new Map(current).set(agent.id, !open))}><Icon name="chevron" size={12} /></button>}</span>
        <AgentRow agent={agent} selected={selected === agent.id} inspect={() => inspect(agent.id)} settings={() => configure(agent.id)} arrivalAt={arrivals[agent.id]} />
      </div>
      {!!descendants.length && (open ? <ol className="team-tree-children">
        {descendants.slice(0, limit).map(child => branch(child, depth + 1))}
        {descendants.length > limit && <li className="team-more" data-more-parent={agent.id}><button type="button" onClick={() => setLimits(current => new Map(current).set(agent.id, limit + 6))}>More agents <span>{descendants.length - limit}</span></button></li>}
      </ol> : <button className="team-collapsed-count" type="button" onClick={() => setExpanded(current => new Map(current).set(agent.id, true))}>{descendants.length} nested agent{descendants.length === 1 ? "" : "s"}</button>)}
    </li>;
  }
  const available = profiles.filter(profile => !profile.used);
  return <div className="team-tree-scroll" ref={tree}>
    {error && <p className="team-load-error" role="status">{error}<button type="button" onClick={refresh}>Retry</button></p>}
    <ol className="team-tree" aria-label="Agent conversations">{agents[0] && branch(agents[0], 0)}</ol>
    {!!available.length && <div className="team-profiles"><span className="team-profiles-label">Available agents</span>
      {available.slice(0, profileLimit).map(profile => <button type="button" key={profile.id} className={`team-profile${selected === profileTarget(profile.id) ? " is-selected" : ""}`}
        onClick={() => inspect(profileTarget(profile.id))}><span><strong>{profile.name}</strong><span>Not started</span></span><small>{profile.description || [profile.provider, profile.model].filter(Boolean).join(" · ")}</small></button>)}
      {available.length > profileLimit && <button type="button" className="team-profile-more" data-more-profiles="" onClick={() => setProfileLimit(value => value + 6)}>More agents <span>{available.length - profileLimit}</span></button>}
    </div>}
  </div>;
}

export function useAgentArrivals(tasks: readonly { id: string; started_at?: number }[], sessionId: string, loaded: boolean): ArrivalTimes {
  const previous = useRef<{ session: string; hydrated: boolean; ids: Set<string> }>({ session: sessionId, hydrated: false, ids: new Set() });
  const [arrivals, setArrivals] = useState<{ session: string; times: ArrivalTimes }>({ session: sessionId, times: {} });
  const identity = tasks.map(task => task.id).join("\u0000");
  useEffect(() => {
    if (previous.current.session !== sessionId) { previous.current = { session: sessionId, hydrated: false, ids: new Set() }; setArrivals({ session: sessionId, times: {} }); }
    if (!loaded) return;
    if (!previous.current.hydrated) { previous.current.hydrated = true; previous.current.ids = new Set(tasks.map(task => task.id)); return; }
    const now = Date.now();
    const fresh = tasks.filter(task => !previous.current.ids.has(task.id) && task.started_at && now - task.started_at * 1000 >= -1000 && now - task.started_at * 1000 < 5000);
    previous.current.ids = new Set(tasks.map(task => task.id));
    if (!fresh.length) return;
    setArrivals(current => ({ session: sessionId, times: { ...(current.session === sessionId ? current.times : {}), ...Object.fromEntries(fresh.map(task => [task.id, now])) } }));
  }, [identity, sessionId, loaded]);
  useEffect(() => {
    const times = Object.values(arrivals.times);
    if (!times.length) return;
    const timer = setTimeout(() => setArrivals(current => ({ ...current, times: Object.fromEntries(Object.entries(current.times).filter(([, at]) => Date.now() - at < 1500)) })), Math.max(0, Math.min(...times) + 1500 - Date.now()));
    return () => clearTimeout(timer);
  }, [arrivals]);
  return arrivals.session === sessionId ? arrivals.times : {};
}
