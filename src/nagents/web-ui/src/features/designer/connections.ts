import type { Design, Invocation, Port } from "./types.js";
import type { Point } from "./viewport.js";

export const PORTS: Port[] = ["top", "right", "bottom", "left"];
export interface Endpoint { agent: string; port: Port; offset: number }
export interface LinkRef { source: string; target: string }
export interface Anchor extends Point { dx: number; dy: number }

export function anchor(position: Point, port: Port, offset = .5): Anchor {
  switch (port) {
    case "top": return { x: position.x + 180 * offset, y: position.y, dx: 0, dy: -1 };
    case "bottom": return { x: position.x + 180 * offset, y: position.y + 68, dx: 0, dy: 1 };
    case "left": return { x: position.x, y: position.y + 68 * offset, dx: -1, dy: 0 };
    case "right": return { x: position.x + 180, y: position.y + 68 * offset, dx: 1, dy: 0 };
  }
}

export function nearestEndpoint(agent: string, position: Point, point: Point): Endpoint {
  const clamp = (value: number) => Math.round(Math.max(0, Math.min(1, value)) * 10000) / 10000;
  return PORTS.map((port) => {
    const offset = clamp(port === "top" || port === "bottom" ? (point.x - position.x) / 180 : (point.y - position.y) / 68);
    const target = anchor(position, port, offset);
    return { endpoint: { agent, port, offset }, distance: Math.hypot(point.x - target.x, point.y - target.y) };
  }).sort((a, b) => a.distance - b.distance)[0].endpoint;
}

export function linkPath(from: Anchor, to: Anchor): string {
  const reach = Math.max(40, Math.min(160, Math.hypot(to.x - from.x, to.y - from.y) / 2));
  return `M ${from.x} ${from.y} C ${from.x + from.dx * reach} ${from.y + from.dy * reach}, ${to.x + to.dx * reach} ${to.y + to.dy * reach}, ${to.x} ${to.y}`;
}

export function endpoints(source: string, edge: Invocation): [Endpoint, Endpoint] {
  return [{ agent: source, port: edge.source_port || "right", offset: edge.source_offset ?? .5 },
    { agent: edge.agent, port: edge.target_port || "left", offset: edge.target_offset ?? .5 }];
}

export function connect(design: Design, from: Endpoint, to: Endpoint, previous?: LinkRef): Design {
  if (!design.agents[from.agent] || !design.agents[to.agent]) return design;
  const old = previous && design.agents[previous.source]?.invokes.find((edge) => edge.agent === previous.target);
  if (previous && !old) return design;
  // One runtime permission per target. A rejected reconnection retains its old edge.
  if (design.agents[from.agent].invokes.some((edge) => edge.agent === to.agent && !(previous?.source === from.agent && edge === old))) return design;
  const next = structuredClone(design);
  if (previous) next.agents[previous.source].invokes = next.agents[previous.source].invokes.filter((edge) => edge.agent !== previous.target);
  next.agents[from.agent].invokes.push({ ...old, agent: to.agent, description: old?.description || "",
    source_port: from.port, source_offset: from.offset, target_port: to.port, target_offset: to.offset });
  return next;
}
