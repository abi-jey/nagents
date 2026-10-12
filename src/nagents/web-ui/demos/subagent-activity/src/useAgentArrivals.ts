import { useEffect, useRef, useState } from "react";
import { visibleAgents } from "./engine";
import type { ArrivalTimes } from "./AgentLifecycleView";
import type { ActivityState } from "./types";

const ARRIVAL_MS = 1400;
/** Wall-clock motion lifetime owned above variant and responsive remounts. */
export function useAgentArrivals(state: ActivityState, generation: number): { arrivalTimes: ArrivalTimes; arrivalAnnouncement: string } {
  const [arrivals, setArrivals] = useState<ArrivalTimes>({});
  const [announcement, setAnnouncement] = useState("");
  const observed = useRef({ generation, ids: new Set<string>() });
  const ids = visibleAgents(state).map(agent => agent.definition.id).join("|");
  useEffect(() => {
    if (observed.current.generation !== generation) {
      observed.current = { generation, ids: new Set<string>() };
      setArrivals({});
      setAnnouncement("");
    }
    const added = visibleAgents(state).filter(agent => !observed.current.ids.has(agent.definition.id) && state.now - agent.definition.spawnedAt <= 2);
    observed.current.ids = new Set(ids ? ids.split("|") : []);
    if (added.length) {
      const at = Date.now();
      setArrivals(current => ({ ...current, ...Object.fromEntries(added.map(agent => [agent.definition.id, at])) }));
      setAnnouncement(`${added.map(agent => agent.definition.name).join(", ")} started. Agent tasks and current states are available in the conversation.`);
    }
  }, [ids, generation]);
  useEffect(() => {
    const times = Object.values(arrivals);
    if (!times.length) return;
    const timer = window.setTimeout(() => setArrivals(current => Object.fromEntries(Object.entries(current).filter(([, at]) => Date.now() - at < ARRIVAL_MS))), Math.max(0, Math.min(...times) + ARRIVAL_MS - Date.now()));
    return () => window.clearTimeout(timer);
  }, [arrivals]);
  return { arrivalTimes: arrivals, arrivalAnnouncement: announcement };
}
