import { useRef, useState } from "react";
import type { Design } from "./types";

export function Canvas({ design, selected, select, change, runningAgents }: {
  design: Design; selected: string; select: (id: string) => void;
  change: (design: Design) => void; runningAgents: Set<string>;
}) {
  const drag = useRef("");
  const previous = useRef({ x: 0, y: 0 });
  const [view, setView] = useState({ x: 0, y: 0, width: 800, height: 520 });
  const [linking, setLinking] = useState("");
  const ids = Object.keys(design.agents);
  const position = (id: string) => design.layout[id] || { x: 90 + (ids.indexOf(id) % 3) * 245, y: 140 + Math.floor(ids.indexOf(id) / 3) * 120 };
  return <div className="designer-canvas">
    <div className="designer-tabs"><button aria-label="Zoom in" title="Zoom in" disabled={view.width < 250} onClick={() => setView({ ...view, width: view.width / 1.25, height: view.height / 1.25 })}>+</button><button aria-label="Zoom out" title="Zoom out" disabled={view.width > 12000} onClick={() => setView({ ...view, width: view.width * 1.25, height: view.height * 1.25 })}>−</button><button onClick={() => {
      const width = Math.max(800, ...ids.map((id) => position(id).x + 220));
      const height = Math.max(520, ...ids.map((id) => position(id).y + 120));
      setView({ x: 0, y: 0, width: Math.max(width, height * 800 / 520), height: Math.max(height, width * 520 / 800) });
    }}>Fit</button><button onClick={() => {
      const levels = new Map<string, number>([[design.entrypoint, 0]]), queue = [design.entrypoint];
      for (let i = 0; i < queue.length; i++) for (const edge of design.agents[queue[i]].invokes) if (!levels.has(edge.agent)) { levels.set(edge.agent, levels.get(queue[i])! + 1); queue.push(edge.agent); }
      const rows = new Map<number, number>(), layout: Design["layout"] = {};
      for (const id of ids) { const level = levels.get(id) || 0, row = rows.get(level) || 0; layout[id] = { x: 90 + level * 270, y: 140 + row * 110 }; rows.set(level, row + 1); }
      change({ ...design, layout });
    }}>Arrange</button>{linking && <button onClick={() => setLinking("")}>Cancel link</button>}</div>
    <svg viewBox={`${view.x} ${view.y} ${view.width} ${view.height}`} role="img" aria-label="Agent delegation graph. Select agents using the list or canvas; drag nodes to arrange."
      onPointerDown={(event) => { drag.current = "__pan"; previous.current = { x: event.clientX, y: event.clientY }; event.currentTarget.setPointerCapture(event.pointerId); }}
      onPointerMove={(event) => {
        if (!drag.current) return;
        const svg = event.currentTarget, point = svg.createSVGPoint();
        if (drag.current === "__pan") {
          const bounds = svg.getBoundingClientRect();
          setView({ ...view, x: view.x - (event.clientX - previous.current.x) * view.width / bounds.width, y: view.y - (event.clientY - previous.current.y) * view.height / bounds.height });
          previous.current = { x: event.clientX, y: event.clientY }; return;
        }
        point.x = event.clientX; point.y = event.clientY;
        const matrix = svg.getScreenCTM(); if (!matrix) return;
        const local = point.matrixTransform(matrix.inverse());
        change({ ...design, layout: { ...design.layout, [drag.current]: { x: Math.max(0, local.x - 90), y: Math.max(0, local.y - 35) } } });
      }} onPointerUp={() => { drag.current = ""; }} onPointerCancel={() => { drag.current = ""; }}>
      <defs><marker id="designer-arrow" markerWidth="8" markerHeight="8" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z" /></marker><pattern id="designer-dots" width="20" height="20" patternUnits="userSpaceOnUse"><circle className="designer-grid-dot" cx="1" cy="1" r=".8" /></pattern></defs>
      <rect x={view.x} y={view.y} width={view.width} height={view.height} fill="url(#designer-dots)" />
      {ids.flatMap((id) => design.agents[id].invokes.map((edge) => {
        const a = position(id), b = position(edge.agent);
        return <g key={`${id}:${edge.agent}`}><path className="designer-edge" markerEnd="url(#designer-arrow)"
          d={`M ${a.x + 180} ${a.y + 34} C ${a.x + 230} ${a.y + 34}, ${b.x - 50} ${b.y + 34}, ${b.x - 5} ${b.y + 34}`}><title>{`${id} delegates to ${edge.agent}: ${edge.description}`}</title></path><text className="designer-edge-label" x={(a.x + 180 + b.x) / 2} y={(a.y + b.y) / 2 + 25} textAnchor="middle">delegate</text></g>;
      }))}
      {ids.map((id) => {
        const p = position(id), agent = design.agents[id];
        return <g key={id} transform={`translate(${p.x},${p.y})`} className={`designer-node ${selected === id ? "selected" : ""} ${runningAgents.has(id) ? "running" : ""}`}
          onPointerDown={(event) => { event.stopPropagation(); select(id);
            if (linking) { const source = design.agents[linking]; if (source && !source.invokes.some((edge) => edge.agent === id)) change({ ...design, agents: { ...design.agents, [linking]: { ...source, invokes: [...source.invokes, { agent: id, description: "" }] } } }); setLinking(""); return; }
            drag.current = id; event.currentTarget.ownerSVGElement?.setPointerCapture(event.pointerId); }}>
          <rect width="180" height="68" rx="5" />
          <circle className="designer-node-mark" cx="14" cy="18" r="3" />
          <text x="24" y="22">{(agent.name || id).slice(0, 20)}</text>
          <text className="designer-node-detail" x="12" y="40">{(design.providers[agent.provider || design.defaults.provider]?.model || "Select provider").slice(0, 25)}</text>
          <text className="designer-node-detail" x="12" y="56">{id === design.entrypoint ? "Entry · " : ""}{agent.tools.length} tools · {agent.mcp.length} MCPs</text>
          <circle className="designer-port" cx="0" cy="34" r="3" />
          <circle className="designer-port" cx="180" cy="34" r="4" onPointerDown={(event) => { event.stopPropagation(); setLinking(id); }}><title>Create delegation link from {id}: click another agent</title></circle>
        </g>;
      })}
    </svg>
    <p>{linking ? `Connect ${linking} → click a target agent` : "Drag to arrange · Drag background to pan · Click a port to connect"}</p>
  </div>;
}
