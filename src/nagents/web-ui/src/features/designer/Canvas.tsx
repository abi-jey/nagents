import { useLayoutEffect, useRef, useState } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import type { Design } from "./types";
import { PORTS, anchor, connect, endpoints, linkPath, nearestEndpoint, type Anchor, type Endpoint, type LinkRef } from "./connections";
import { fitViewport, panViewport, resizeViewport, zoomViewport, type Point, type Viewport } from "./viewport";

interface ConnectionDraft { fixed: Endpoint; moving: "source" | "target"; previous?: LinkRef; cursor: Anchor; start: Point; moved: boolean; dragging: boolean }

export function Canvas({ design, selected, select, change, runningAgents }: {
  design: Design; selected: string; select: (id: string) => void;
  change: (design: Design) => void; runningAgents: Set<string>;
}) {
  const drag = useRef("");
  const previous = useRef({ x: 0, y: 0 });
  const offset = useRef({ x: 0, y: 0 });
  const svgRef = useRef<SVGSVGElement>(null);
  const [viewport, setViewport] = useState<Viewport>({ x: 0, y: 0, width: 0, height: 0, scale: 1 });
  const view = { x: viewport.x, y: viewport.y, width: Math.max(1, viewport.width) / viewport.scale, height: Math.max(1, viewport.height) / viewport.scale };
  useLayoutEffect(() => {
    const svg = svgRef.current;
    if (!svg) return;
    const resize = () => { const bounds = svg.getBoundingClientRect(); setViewport((current) => resizeViewport(current, bounds.width, bounds.height)); };
    resize();
    const observer = new ResizeObserver(resize);
    observer.observe(svg);
    return () => observer.disconnect();
  }, []);
  const [draft, setDraft] = useState<ConnectionDraft>();
  const draftRef = useRef<ConnectionDraft | undefined>(undefined);
  const [selectedLink, setSelectedLink] = useState<LinkRef>();
  const [message, setMessage] = useState("");
  const ids = Object.keys(design.agents);
  const position = (id: string) => design.layout[id] || { x: 90 + (ids.indexOf(id) % 3) * 245, y: 140 + Math.floor(ids.indexOf(id) / 3) * 120 };
  function updateDraft(value?: ConnectionDraft) { draftRef.current = value; setDraft(value); }
  useLayoutEffect(() => {
    setViewport((current) => fitViewport(current, ids.map(position)));
    updateDraft(); setSelectedLink(undefined);
  }, [design.id]);
  function localPoint(clientX: number, clientY: number): Point {
    const svg = svgRef.current, matrix = svg?.getScreenCTM();
    if (!svg || !matrix) return { x: 0, y: 0 };
    const point = svg.createSVGPoint(); point.x = clientX; point.y = clientY;
    return point.matrixTransform(matrix.inverse());
  }
  function finishConnection(endpoint: Endpoint) {
    const current = draftRef.current;
    if (!current) return;
    const from = current.moving === "source" ? endpoint : current.fixed;
    const to = current.moving === "target" ? endpoint : current.fixed;
    const next = connect(design, from, to, current.previous);
    if (next !== design) { change(next); setSelectedLink({ source: from.agent, target: to.agent }); setMessage(""); }
    else setMessage("That delegation already exists. The original link was kept.");
    updateDraft();
  }
  function beginConnection(endpoint: Endpoint, event?: ReactPointerEvent<SVGElement>, reconnect?: { link: LinkRef; moving: "source" | "target"; fixed: Endpoint }) {
    event?.stopPropagation();
    if (draftRef.current && !draftRef.current.dragging && !reconnect) { finishConnection(endpoint); return; }
    setMessage(""); drag.current = "";
    const cursor = anchor(position(endpoint.agent), endpoint.port, endpoint.offset);
    updateDraft({ fixed: reconnect?.fixed || endpoint, moving: reconnect?.moving || "target", previous: reconnect?.link,
      cursor, start: { x: event?.clientX || 0, y: event?.clientY || 0 }, moved: false, dragging: !!event });
    if (event) svgRef.current?.setPointerCapture(event.pointerId);
  }
  function release(event: ReactPointerEvent<SVGSVGElement>) {
    drag.current = "";
    const current = draftRef.current;
    if (!current?.dragging) return;
    if (!current.moved) { updateDraft({ ...current, dragging: false }); return; }
    const hit = document.elementFromPoint(event.clientX, event.clientY);
    const id = hit?.closest("[data-agent-id]")?.getAttribute("data-agent-id");
    if (id && design.agents[id]) finishConnection(nearestEndpoint(id, position(id), localPoint(event.clientX, event.clientY)));
    else updateDraft(); // Drop on empty space cancels; it never deletes an existing edge.
  }
  const activeEdge = selectedLink && design.agents[selectedLink.source]?.invokes.find((edge) => edge.agent === selectedLink.target);
  const activeEnds = selectedLink && activeEdge ? endpoints(selectedLink.source, activeEdge) : [];
  const fixed = draft && anchor(position(draft.fixed.agent), draft.fixed.port, draft.fixed.offset);
   const cursor = draft?.cursor;
  return <div className="designer-canvas">
    <div className="designer-tabs"><button aria-label="Zoom in" title="Zoom in" disabled={viewport.scale >= 4} onClick={() => setViewport((current) => zoomViewport(current, 1.25))}>+</button><button aria-label="Zoom out" title="Zoom out" disabled={viewport.scale <= .05} onClick={() => setViewport((current) => zoomViewport(current, .8))}>−</button><button onClick={() => setViewport((current) => fitViewport(current, ids.map(position)))}>Fit</button><button onClick={() => {
      const levels = new Map<string, number>([[design.entrypoint, 0]]), queue = [design.entrypoint];
      for (let i = 0; i < queue.length; i++) for (const edge of design.agents[queue[i]].invokes) if (!levels.has(edge.agent)) { levels.set(edge.agent, levels.get(queue[i])! + 1); queue.push(edge.agent); }
      const rows = new Map<number, number>(), layout: Design["layout"] = {};
      for (const id of ids) { const level = levels.get(id) || 0, row = rows.get(level) || 0; layout[id] = { x: 90 + level * 270, y: 140 + row * 110 }; rows.set(level, row + 1); }
      change({ ...design, layout });
    }}>Arrange</button>{draft && <button onClick={() => updateDraft()}>Cancel link</button>}{selectedLink && activeEdge && <button onClick={() => { change({ ...design, agents: { ...design.agents, [selectedLink.source]: { ...design.agents[selectedLink.source], invokes: design.agents[selectedLink.source].invokes.filter((edge) => edge !== activeEdge) } } }); setSelectedLink(undefined); }}>Remove link</button>}</div>
    <svg ref={svgRef} viewBox={`${view.x} ${view.y} ${view.width} ${view.height}`} preserveAspectRatio="none" role="group" aria-label="Agent delegation graph"
      onKeyDown={(event) => { if (event.key === "Escape") { updateDraft(); drag.current = ""; } }}
      onPointerDown={(event) => { if (draftRef.current) { updateDraft(); return; } setSelectedLink(undefined); drag.current = "__pan"; previous.current = { x: event.clientX, y: event.clientY }; event.currentTarget.setPointerCapture(event.pointerId); }}
      onPointerMove={(event) => {
        const connection = draftRef.current;
         if (connection) {
           const point = localPoint(event.clientX, event.clientY);
           const hit = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-agent-id]")?.getAttribute("data-agent-id");
           const target = hit && design.agents[hit] ? nearestEndpoint(hit, position(hit), point) : undefined;
           const cursor = target ? anchor(position(target.agent), target.port, target.offset) : { ...point, dx: 0, dy: 0 };
           updateDraft({ ...connection, cursor, moved: connection.moved || Math.hypot(event.clientX - connection.start.x, event.clientY - connection.start.y) > 4 }); return;
         }
        if (!drag.current) return;
        if (drag.current === "__pan") {
          const dx = event.clientX - previous.current.x, dy = event.clientY - previous.current.y;
          setViewport((current) => panViewport(current, dx, dy)); previous.current = { x: event.clientX, y: event.clientY }; return;
        }
        const point = localPoint(event.clientX, event.clientY);
        change({ ...design, layout: { ...design.layout, [drag.current]: { x: point.x - offset.current.x, y: point.y - offset.current.y } } });
      }} onPointerUp={release} onPointerCancel={() => { drag.current = ""; updateDraft(); }}>
      <defs><marker id="designer-arrow" markerWidth="8" markerHeight="8" refX="8" refY="3" orient="auto"><path d="M0,0 L0,6 L8,3 z" /></marker><pattern id="designer-dots" width="20" height="20" patternUnits="userSpaceOnUse"><circle className="designer-grid-dot" cx="1" cy="1" r=".8" /></pattern></defs>
      <rect x={view.x} y={view.y} width={view.width} height={view.height} fill="url(#designer-dots)" />
      {ids.flatMap((id) => design.agents[id].invokes.map((edge) => {
         const [from, to] = endpoints(id, edge);
         const reconnecting = draft?.previous?.source === id && draft.previous.target === edge.agent;
         const a = reconnecting && draft.moving === "source" ? draft.cursor : anchor(position(id), from.port, from.offset);
         const b = reconnecting && draft.moving === "target" ? draft.cursor : anchor(position(edge.agent), to.port, to.offset);
        const selected = selectedLink?.source === id && selectedLink.target === edge.agent;
        return <g key={`${id}:${edge.agent}`} className={selected ? "designer-link selected" : "designer-link"}>
           <path className="designer-edge-hit" style={reconnecting ? { pointerEvents: "none" } : undefined} d={linkPath(a, b)} role="button" tabIndex={0} aria-label={`Delegation ${id} to ${edge.agent}`} aria-pressed={selected}
            onPointerDown={(event) => { event.stopPropagation(); updateDraft(); setSelectedLink({ source: id, target: edge.agent }); }}
            onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); setSelectedLink({ source: id, target: edge.agent }); } }} />
          <path className="designer-edge" pointerEvents="none" markerEnd="url(#designer-arrow)" d={linkPath(a, b)}><title>{edge.description}</title></path>
          <text className="designer-edge-label" pointerEvents="none" x={(a.x + b.x) / 2} y={(a.y + b.y) / 2 - 9} textAnchor="middle">delegate</text>
        </g>;
      }))}
       {fixed && cursor && draft && !draft.previous && <path className="designer-edge-preview" pointerEvents="none" markerEnd="url(#designer-arrow)" d={draft.moving === "target" ? linkPath(fixed, cursor) : linkPath(cursor, fixed)} />}
      {ids.map((id) => {
        const p = position(id), agent = design.agents[id];
        return <g key={id} data-agent-id={id} transform={`translate(${p.x},${p.y})`} className={`designer-node ${selected === id ? "selected" : ""} ${runningAgents.has(id) ? "running" : ""}`}
          onPointerDown={(event) => { event.stopPropagation(); select(id);
            const local = localPoint(event.clientX, event.clientY);
            if (draftRef.current) { finishConnection(nearestEndpoint(id, p, local)); return; }
            setSelectedLink(undefined); offset.current = { x: local.x - p.x, y: local.y - p.y };
            drag.current = id; svgRef.current?.setPointerCapture(event.pointerId); }}>
          <rect width="180" height="68" rx="5" />
          <circle className="designer-node-mark" cx="14" cy="18" r="3" />
          <text x="24" y="22">{(agent.name || id).slice(0, 20)}</text>
          <text className="designer-node-detail" x="12" y="40">{(design.providers[agent.provider || design.defaults.provider]?.model || "Select provider").slice(0, 25)}</text>
          <text className="designer-node-detail" x="12" y="56">{id === design.entrypoint ? "Entry · " : ""}{agent.tools.length} tools · {agent.mcp.length} MCPs</text>
          {PORTS.map((port) => { const point = anchor({ x: 0, y: 0 }, port); return <g key={port} data-port={port} className="designer-port" role="button" tabIndex={0} aria-label={`${id} ${port} connection`}
            onPointerDown={(event) => beginConnection({ agent: id, port, offset: .5 }, event)}
            onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); beginConnection({ agent: id, port, offset: .5 }); } }}>
            <circle className="designer-port-hit" cx={point.x} cy={point.y} r="10" /><circle className="designer-port-dot" cx={point.x} cy={point.y} r="4" />
            <title>Connect from {id}'s {port} side</title></g>; })}
        </g>;
      })}
      {selectedLink && activeEnds.length === 2 && activeEnds.map((endpoint, index) => {
         const moving = index === 0 ? "source" : "target";
         const reconnecting = !!draft?.previous && draft.moving === moving;
         const point = reconnecting ? draft.cursor : anchor(position(endpoint.agent), endpoint.port, endpoint.offset);
         return <circle key={moving} pointerEvents={draft?.previous ? "none" : undefined} data-agent-id={endpoint.agent} className="designer-reconnect" cx={point.x} cy={point.y} r="7" role="button" tabIndex={0}
          aria-label={`Reconnect ${moving} of ${selectedLink.source} to ${selectedLink.target}`}
          onPointerDown={(event) => beginConnection(endpoint, event, { link: selectedLink, moving, fixed: activeEnds[1 - index] })}
          onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); beginConnection(endpoint, undefined, { link: selectedLink, moving, fixed: activeEnds[1 - index] }); } }}><title>Drag to reconnect {moving}</title></circle>;
      })}
    </svg>
    <p role="status">{draft ? `${draft.previous ? "Reconnect" : "Connect"} ${draft.moving} → drop on any agent edge, or click a port · Escape cancels` : message || "Drag any port to connect · Select a link to reconnect either end"}</p>
  </div>;
}
