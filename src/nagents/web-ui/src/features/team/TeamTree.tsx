import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, type CSSProperties, type ReactNode } from "react";
import { Icon } from "../../components/Icon.js";
import { ProviderLogo } from "../../components/ProviderLogo.js";
import { contextPercent, contextSummary, contextUsage, contextUsageLabel, contextWindowSource } from "../context/controller.js";
import { compactionMarker, compactionWarning } from "./context.js";
import { agentAncestors, orderedTeam, phaseLabel, providerLabel, type TeamAgent, type TeamProfile } from "./model.js";
import { isProfileTarget, profileSelection, profileTarget } from "./useTeam.js";
import { bucketKey, definitionGroupKey, TeamGroups, teamBucket, type DefinitionGroup, type TeamBucket } from "./grouping.js";
import { scrollWithin } from "../../components/scrollWithin.js";
import { remoteAuthLabel } from "./RemoteStatus.js";
import { RemoteConnectionChip } from "./RemoteConnectionChip.js";

export type ArrivalTimes = Readonly<Record<string, number>>;
export function useArrivalStyle(at = 0): CSSProperties {
  return useMemo(() => at ? { "--arrival-delay": `${Math.min(0, at - Date.now())}ms` } as CSSProperties : {}, [at]);
}

export function TeamIcon({ size = 18 }: { size?: number }) {
  return <svg width={size} height={size} viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.55" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><circle cx="10" cy="5.5" r="2.4" /><path d="M6 17v-1.8a4 4 0 0 1 8 0V17M4.2 5a2.2 2.2 0 0 0 0 4.4M15.8 5a2.2 2.2 0 0 1 0 4.4M2 16v-1.3a3 3 0 0 1 1.8-2.7M18 16v-1.3a3 3 0 0 0-1.8-2.7" /></svg>;
}

export function AgentState({ agent }: { agent: TeamAgent }) {
  if (agent.providerKind === "a2a") return <span className={`team-state state-${agent.phase} remote-agent-state`} title={agent.phase === "waiting" ? "Waiting" : phaseLabel(agent)}>
    {agent.phase === "running" ? <span className="remote-running-mark" aria-hidden="true" /> : agent.phase === "waiting" ? <span className="remote-waiting-mark" aria-hidden="true"><i /><i /></span> : agent.phase === "done" ? <svg width="12" height="12" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="m4 10 4 4 8-8" /></svg> : <i aria-hidden="true" />}
    {agent.phase === "waiting" ? "Waiting" : phaseLabel(agent)}
  </span>;
  return <span className={`team-state state-${agent.phase}`} title={agent.reason}><i aria-hidden="true" />{phaseLabel(agent)}</span>;
}

export function AgentRow({ agent, selected, inspect, settings, connection, arrivalAt = 0, grouped = false, connectionInGroup = false }: {
  agent: TeamAgent; selected: boolean; inspect: () => void; settings: () => void; connection?: () => void; arrivalAt?: number;
  grouped?: boolean; connectionInGroup?: boolean;
}) {
  const remote = agent.providerKind === "a2a";
  const context = remote ? undefined : agent.context;
  const marker = compactionMarker(agent);
  const warning = compactionWarning(agent);
  const provider = providerLabel(agent);
  const motion = useArrivalStyle(arrivalAt);
  const hasDefinition = !!agent.id && !!agent.profile;
  return <article className={`team-agent-row${remote ? " is-remote" : ""}${selected ? " is-selected" : ""}${arrivalAt ? " is-arriving" : ""}`} style={motion} data-agent-id={agent.id || "main"}>
    <button type="button" className="team-agent-open" onClick={inspect} aria-current={selected ? "page" : undefined} aria-description={[marker?.description, agent.providerKind === "a2a" && remoteAuthLabel(agent.authentication)].filter(Boolean).join(". ") || undefined}
      aria-label={`Open ${agent.name} conversation. ${phaseLabel(agent)}${agent.profile ? `. Definition ${agent.profile}` : ""}${provider ? `. ${provider} ${agent.model}` : ""}`}>
      <span className="team-agent-heading"><span className="team-agent-identity"><strong className={grouped ? "team-agent-run-name" : hasDefinition ? "team-agent-definition" : undefined} title={hasDefinition ? `Agent definition: ${agent.profile}` : undefined}>{grouped ? agent.name : hasDefinition ? agent.profile : agent.name}</strong>{!grouped && hasDefinition && <span className="team-agent-instance" title={`Conversation: ${agent.name}`}>{agent.name}</span>}</span><AgentState agent={agent} /></span>
      {!remote && <><span className="team-agent-model">{provider && <ProviderLogo provider={agent.providerKind} />}<span>{[provider, agent.model].filter(Boolean).join(" · ") || "Configuration unavailable"}</span></span>
      <span className="team-agent-context" title={context ? `${contextUsageLabel(context)}: ${contextUsage(context).toLocaleString()} tokens; ${contextWindowSource(context)}${marker ? `; ${marker.description}` : ""}` : undefined}>
        {context ? contextSummary(context) : "Context unavailable"}
      </span>
      {context?.context_window ? <span className="team-agent-meter" aria-hidden="true"><i style={{ width: `${contextPercent(context)}%` }} />
        {marker && <b className="team-agent-threshold" style={{ left: `${marker.percent}%` }} title={marker.description} />}
      </span> : null}
      {warning && <span className="team-context-warning">{warning}</span>}</>}
    </button>
    {remote && !connectionInGroup && <div className="team-agent-connection"><RemoteConnectionChip authentication={agent.authentication} name={agent.profile || agent.name} activate={connection || settings} /></div>}
    <button type="button" className="team-agent-settings" aria-label={`Settings for ${agent.name}`} onClick={settings}><Icon name="settings" size={14} /></button>
  </article>;
}

export function TeamTree({ agents, profiles, selected, inspect, configure, arrivals, error, refresh }: {
  agents: TeamAgent[]; profiles: TeamProfile[]; selected: string; inspect: (id: string) => void; configure: (id: string) => void;
  arrivals: ArrivalTimes; error: string; refresh: () => void;
}) {
  const treeId = useId();
  const { children } = useMemo(() => orderedTeam(agents), [agents]);
  const known = new Map(agents.map(agent => [agent.id, agent]));
  const catalog = new Map(profiles.map(profile => [profile.id, profile]));
  const grouping = useRef(new TeamGroups());
  const groups = new Map([...children].map(([parent, direct]) => [parent, grouping.current.update(parent, direct)]));
  const [expanded, setExpanded] = useState(new Map<string, boolean>());
  const [limits, setLimits] = useState(new Map<string, number>());
  const [startProfile, setStartProfile] = useState("");
  const [search, setSearch] = useState("");
  const revealed = useRef(new Set<string>());
  const selection = useRef({ id: "", resolved: false });
  const tree = useRef<HTMLDivElement>(null);
  const positions = useRef(new Map<string, { top: number; left: number }>());
  const selectedScope = isProfileTarget(selected) ? profileSelection(selected) : { profile: "", parentId: selected };
  const focusedPath = new Set([selectedScope.parentId, ...agentAncestors(selectedScope.parentId, agents)]);
  const startParent = known.get(selectedScope.parentId);
  const chosen = catalog.has(startProfile) ? startProfile : catalog.has(selectedScope.profile) ? selectedScope.profile :
    profiles.find(profile => permission(startParent, profile.id).allowed)?.id || profiles[0]?.id || "";

  function permission(parent: TeamAgent | undefined, profile: string): { allowed: boolean; reason: string } {
    const definition = catalog.get(profile);
    if (!parent) return { allowed: false, reason: "This parent runtime is unavailable." };
    if (!definition) return { allowed: false, reason: "This agent definition is unavailable." };
    if (!parent.id) return { allowed: definition.can_start ?? definition.available, reason: definition.unavailable_reason || "This profile is unavailable from the current agent." };
    const allowed = parent.task?.can_start_children === true && parent.task.child_agents?.includes(profile) === true;
    return { allowed, reason: allowed ? "" : parent.task?.child_start_unavailable_reason || "This definition is unavailable under the parent's policy." };
  }
  const startPermission = permission(startParent, chosen);
  const query = search.trim().toLowerCase();
  function matchesTree(agent: TeamAgent, seen = new Set<string>()): boolean {
    if (seen.has(agent.id)) return false;
    if (!query || [agent.name, agent.profile, catalog.get(agent.profile)?.name || ""].some(value => value.toLowerCase().includes(query))) return true;
    const visited = new Set(seen).add(agent.id);
    return (children.get(agent.id) || []).some(child => matchesTree(child, visited));
  }
  function toggle(key: string, open: boolean) { setExpanded(current => new Map(current).set(key, !open)); }

  useEffect(() => { if (isProfileTarget(selected)) setStartProfile(profileSelection(selected).profile); }, [selected]);

  useEffect(() => {
    const target = isProfileTarget(selected) ? profileSelection(selected).parentId : selected;
    const resolved = known.has(target);
    if (selection.current.id === selected && (selection.current.resolved || !resolved)) return;
    selection.current = { id: selected, resolved };
    if (!resolved) return;
    // Only explicit recipient changes reveal a path. Stream and status updates
    // leave the user's collapse choices untouched.
    const path = [target, ...agentAncestors(target, agents)];
    setExpanded(current => {
      const next = new Map(current);
      for (const id of path) {
        next.set(id, true); revealed.current.add(id);
        const parent = [...children].find(([, direct]) => direct.some(agent => agent.id === id))?.[0];
        const agent = known.get(id);
        if (parent !== undefined && agent) {
          const key = definitionGroupKey(parent, agent.profile);
          next.set(key, true); next.set(bucketKey(key, teamBucket(agent)), true);
        }
      }
      return next;
    });
    requestAnimationFrame(() => {
      const row = [...(tree.current?.querySelectorAll<HTMLElement>("[data-agent-id]") || [])].find(element => element.dataset.agentId === (target || "main"));
      if (row && tree.current) scrollWithin(tree.current, row);
    });
  }, [selected, agents]);

  useLayoutEffect(() => {
    const region = tree.current, view = region?.ownerDocument.defaultView;
    if (!region || !view) return;
    const rect = region.getBoundingClientRect();
    const previous = positions.current, next = new Map<string, { top: number; left: number }>();
    const motion = typeof view.matchMedia === "function" && !view.matchMedia("(prefers-reduced-motion: reduce)").matches;
    for (const row of region.querySelectorAll<HTMLElement>("[data-agent-id]")) {
      const bounds = row.getBoundingClientRect();
      if (!bounds.width || !bounds.height) continue;
      const key = row.dataset.agentId!, point = { top: bounds.top - rect.top + region.scrollTop, left: bounds.left - rect.left };
      next.set(key, point);
      const before = previous.get(key);
      if (motion && before && typeof row.animate === "function" && Math.abs(before.top - point.top) + Math.abs(before.left - point.left) > 1) {
        row.animate([{ transform: `translate(${before.left - point.left}px, ${before.top - point.top}px)` }, { transform: "none" }], { duration: 240, easing: "cubic-bezier(.2,.7,.2,1)" });
      }
    }
    positions.current = next;
  }, [agents, expanded, limits, search]);

  function branch(agent: TeamAgent, depth: number, hidden = false, bucket?: TeamBucket, sharedConnection = false): ReactNode {
    const direct = children.get(agent.id) || [];
    const open = !!query && direct.some(child => matchesTree(child)) || (expanded.get(agent.id) ?? (!agent.id || depth < 2 && teamBucket(agent) === "running"));
    const showConnection = agent.providerKind === "a2a" && catalog.get(agent.profile)?.provider === "a2a";
    return <li key={`run:${agent.id || "main"}`} className="team-tree-branch" data-run-id={agent.id || "main"} data-bucket={bucket} hidden={hidden}>
      <div className="team-tree-row"><span className="team-tree-toggle-slot">{!!direct.length && <button type="button" className="team-tree-toggle" aria-label={`${open ? "Collapse" : "Expand"} ${agent.name}'s agents`} aria-expanded={open}
        onClick={() => toggle(agent.id, open)}><Icon name="chevron" size={12} /></button>}</span>
        <AgentRow agent={agent} grouped={!!agent.id} connectionInGroup={sharedConnection} selected={selected === agent.id} inspect={() => inspect(agent.id)} settings={() => configure(agent.id)} connection={showConnection ? () => configure(profileTarget(agent.profile)) : undefined} arrivalAt={arrivals[agent.id]} />
      </div>
      {!!direct.length && <ol className="team-tree-children team-grouped-children" aria-label={`${agent.name}'s child agents`} hidden={!open}>{(groups.get(agent.id) || []).flatMap(group => groupItems(agent, group, depth + 1, open))}</ol>}
    </li>;
  }

  function groupItems(parent: TeamAgent, group: DefinitionGroup, depth: number, parentOpen: boolean): ReactNode[] {
    const all = [...group.running, ...group.past], definition = catalog.get(group.profile);
    const name = definition?.name || group.profile || "Definition unavailable";
    const matches = (agent: TeamAgent) => name.toLowerCase().includes(query) || matchesTree(agent);
    const matching = !query || all.some(matches);
    const open = !!query || (expanded.get(group.key) ?? (group.running.length > 0 || all.some(agent => focusedPath.has(agent.id))));
    const domId = `${treeId}-${encodeURIComponent(group.key)}`;
    const remote = definition?.provider === "a2a" || !definition && all.every(agent => agent.providerKind === "a2a");
    const connection = remote ? () => configure(definition ? profileTarget(group.profile) : all[0].id) : undefined;
    const allowed = permission(parent, group.profile);
    const fresh = Math.max(0, ...all.map(agent => arrivals[agent.id] || 0));
    const count = group.running.length + group.past.length;
    const result: ReactNode[] = [<li key={`group:${group.key}`} className={`team-definition-group${fresh ? " is-arriving" : ""}`} hidden={!matching} data-parent-id={parent.id} data-profile={group.profile}>
      <div className="team-definition-heading"><button type="button" className="team-definition-toggle" aria-expanded={open} aria-controls={(["running", "past"] as const).filter(bucket => group[bucket].length).map(bucket => `${domId}-${bucket}`).join(" ")} onClick={() => toggle(group.key, open)}>
        <Icon name="chevron" size={11} /><strong>{name}</strong>{!open && <span title={`${group.running.length} running · ${group.past.length} past`}>{count}</span>}
      </button>{remote && <RemoteConnectionChip authentication={definition?.authentication || all[0].authentication} name={name} activate={connection!} />}
        <button type="button" className="team-definition-start" aria-label={`Start ${name} under ${parent.name}`} title={allowed.allowed ? `Start ${name} under ${parent.name}` : allowed.reason} disabled={!allowed.allowed} onClick={() => inspect(profileTarget(group.profile, parent.id))}><Icon name="plus" size={13} /></button>
      </div>
    </li>];
    for (const bucket of ["running", "past"] as const) {
      const items = group[bucket];
      if (!items.length) continue;
      const key = bucketKey(group.key, bucket), bucketOpen = !!query || (expanded.get(key) ?? (bucket === "running" || items.some(agent => focusedPath.has(agent.id))));
      const limit = limits.get(key) ?? (bucket === "running" ? 6 : 2);
      if (query) items.filter(matches).forEach(agent => revealed.current.add(agent.id));
      else if (parentOpen && open && bucketOpen) items.slice(0, limit).forEach(agent => revealed.current.add(agent.id));
      const visible = items.filter(agent => revealed.current.has(agent.id));
      const remaining = items.length - visible.length;
      result.push(<li key={`bucket:${key}`} className="team-run-bucket" hidden={!open || !!query && !items.some(matches)} id={`${domId}-${bucket}`} data-parent-id={parent.id} data-profile={group.profile} data-bucket-heading={bucket}>
        <button type="button" aria-expanded={bucketOpen} onClick={() => toggle(key, bucketOpen)}><Icon name="chevron" size={9} /><span>{bucket === "running" ? "Running" : "Past runs"}</span><span>{query ? items.filter(matches).length : items.length}</span></button>
      </li>);
      for (const agent of visible) result.push(branch(agent, depth, !open || !bucketOpen || !!query && !matches(agent), bucket, remote));
      if (remaining) result.push(<li key={`more:${key}`} className="team-group-more" hidden={!open || !bucketOpen || !!query}><button type="button" onClick={() => setLimits(current => new Map(current).set(key, Math.max(limit, visible.length) + 3))}>Show {remaining} more<Icon name="chevron" size={10} /></button></li>);
    }
    return result;
  }

  return <div className="team-navigation">
    <label className="team-run-search"><Icon name="search" size={13} /><input type="search" aria-label="Find an agent or run" placeholder="Find an agent or run…" value={search} onChange={event => setSearch(event.target.value)} /></label>
    <div className="team-tree-scroll" ref={tree}>
      {error && <p className="team-load-error" role="status">{error}<button type="button" onClick={refresh}>Retry</button></p>}
      <ol className="team-tree team-grouped-tree" aria-label="Agent conversations">{agents[0] && branch(agents[0], 0)}</ol>
    </div>
    {!!profiles.length && <section className="team-start-agent" aria-label="Start an agent">
      <label htmlFor={`${treeId}-start-definition`}>Under <strong>{startParent?.name || "Unavailable parent"}</strong></label>
      <div><select id={`${treeId}-start-definition`} aria-label="Agent definition" value={chosen} onChange={event => setStartProfile(event.target.value)}>
        {profiles.map(profile => <option key={profile.id} value={profile.id}>{profile.name}</option>)}
      </select><button type="button" aria-label={`Start ${catalog.get(chosen)?.name || "agent"} under ${startParent?.name || "parent"}`} title={startPermission.allowed ? "Start a new conversation under this parent" : startPermission.reason} disabled={!startPermission.allowed} onClick={() => inspect(profileTarget(chosen, startParent!.id))}><Icon name="plus" size={13} /><span>Start</span></button></div>
      {!startPermission.allowed && <p>{startPermission.reason}</p>}
    </section>}
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
