import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type PointerEvent, type RefObject } from "react";

export type PanelSide = "sessions" | "team";
export interface PanelWidths { sessions: number; team: number }
export const panelWidthKey = "ngn:panel-widths:v1";
export const panelLimits = {
  sessions: { min: 188, max: 380, initial: 240, rail: 56 },
  team: { min: 232, max: 420, initial: 292, rail: 44 },
};
export const panelGutter = 16;
export const minimumChatWidth = 360;
const initialWidths: PanelWidths = { sessions: 240, team: 292 };

export function parsePanelWidths(raw: string | null): PanelWidths {
  const result = { ...initialWidths };
  try {
    const parsed: unknown = JSON.parse(raw || "null");
    if (!parsed || typeof parsed !== "object" || !("version" in parsed) || parsed.version !== 1) return result;
    for (const side of ["sessions", "team"] as const) {
      const value: unknown = (parsed as Record<string, unknown>)[side];
      if (typeof value === "number" && Number.isFinite(value))
        result[side] = Math.round(Math.max(panelLimits[side].min, Math.min(panelLimits[side].max, value)));
    }
  } catch { /* Storage is an optional browser preference. */ }
  return result;
}

export function fitPanelWidths(preferred: PanelWidths, width: number, sidebarCollapsed: boolean, teamOpen: boolean): PanelWidths {
  const desktopTeam = width > 1100;
  const result = {
    sessions: sidebarCollapsed ? panelLimits.sessions.rail : preferred.sessions,
    team: desktopTeam ? teamOpen ? preferred.team : panelLimits.team.rail : 0,
  };
  if (width <= 760) return result;
  let excess = result.sessions + result.team + panelGutter * (desktopTeam ? 2 : 1) + minimumChatWidth - width;
  for (const side of ["sessions", "team"] as const) {
    if (excess <= 0 || side === "sessions" && sidebarCollapsed || side === "team" && (!desktopTeam || !teamOpen)) continue;
    const reduction = Math.min(excess, Math.max(0, result[side] - panelLimits[side].min));
    result[side] -= reduction; excess -= reduction;
  }
  return result;
}

interface Drag { side: PanelSide; x: number; width: number; preferred: PanelWidths; pointer: number; element: HTMLDivElement; restoreCollapse?: () => void }

export function usePanelLayout(root: RefObject<HTMLElement | null>, sidebarCollapsed: boolean, teamOpen: boolean) {
  const [preferred, setPreferred] = useState(() => {
    try { return parsePanelWidths(window.localStorage.getItem(panelWidthKey)); } catch { return { ...initialWidths }; }
  });
  const [available, setAvailable] = useState(() => typeof window === "undefined" ? 1200 : window.innerWidth);
  const [dragging, setDragging] = useState<PanelSide>();
  const drag = useRef<Drag | undefined>(undefined);
  const effective = fitPanelWidths(preferred, available, sidebarCollapsed, teamOpen);

  function stop(restore = false) {
    const active = drag.current;
    if (!active) return;
    drag.current = undefined;
    if (restore) { setPreferred(active.preferred); active.restoreCollapse?.(); }
    setDragging(undefined);
    if (active.element.hasPointerCapture?.(active.pointer)) active.element.releasePointerCapture(active.pointer);
  }

  useLayoutEffect(() => {
    const element = root.current;
    if (!element) return;
    const measure = () => {
      const width = Math.round(element.getBoundingClientRect().width || window.innerWidth);
      setAvailable(width);
      if (drag.current) stop(true);
    };
    measure();
    const observer = typeof ResizeObserver === "undefined" ? undefined : new ResizeObserver(measure);
    observer?.observe(element);
    window.addEventListener("resize", measure);
    return () => {
      observer?.disconnect(); window.removeEventListener("resize", measure);
      const active = drag.current; drag.current = undefined;
      if (active?.element.hasPointerCapture?.(active.pointer)) active.element.releasePointerCapture(active.pointer);
    };
  }, [root]);

  useEffect(() => {
    if (dragging) return;
    try { window.localStorage.setItem(panelWidthKey, JSON.stringify({ version: 1, ...preferred })); } catch { /* Keep resizing available without storage. */ }
  }, [preferred, dragging]);

  function maximum(side: PanelSide): number {
    const other = side === "sessions" ? effective.team : effective.sessions;
    const gutters = panelGutter * (available > 1100 ? 2 : 1);
    return Math.max(panelLimits[side].min, Math.min(panelLimits[side].max, available - other - minimumChatWidth - gutters));
  }
  function resize(side: PanelSide, width: number) {
    const next = Math.round(Math.max(panelLimits[side].min, Math.min(maximum(side), width)));
    setPreferred(previous => ({ ...previous, [side]: next }));
  }
  function start(event: PointerEvent<HTMLDivElement>, side: PanelSide, collapsed: boolean, setCollapsed: (value: boolean) => void) {
    if (event.button !== 0 || available <= (side === "team" ? 1100 : 760)) return;
    event.preventDefault(); event.currentTarget.focus({ preventScroll: true });
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { side, x: event.clientX, width: collapsed ? preferred[side] : effective[side], preferred, pointer: event.pointerId, element: event.currentTarget,
      restoreCollapse: collapsed ? () => setCollapsed(true) : undefined };
    setDragging(side);
    if (collapsed) setCollapsed(false);
  }
  function move(event: PointerEvent<HTMLDivElement>) {
    const active = drag.current;
    if (active?.pointer !== event.pointerId) return;
    resize(active.side, active.width + (event.clientX - active.x) * (active.side === "sessions" ? 1 : -1));
  }
  function end(event: PointerEvent<HTMLDivElement>, cancel = false) { if (drag.current?.pointer === event.pointerId) stop(cancel); }
  const style = {
    "--layout-sidebar-width": `${effective.sessions}px`,
    "--layout-team-width": `${effective.team}px`,
  } as CSSProperties;
  return { preferred, effective, style, dragging, maximum, resize, start, move, end, cancel: () => stop(true),
    finish: () => stop(), reset: (side: PanelSide) => resize(side, panelLimits[side].initial) };
}
