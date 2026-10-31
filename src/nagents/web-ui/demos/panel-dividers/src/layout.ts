import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from "react";

export type Variant = "a" | "b" | "c";
export type Side = "left" | "right";
export interface Panel { width: number; closed: boolean }
type Layout = Record<Side, Panel>;
const storageKey = "ngn-panel-divider-studies-v1";
export const defaults: Layout = { left: { width: 240, closed: false }, right: { width: 292, closed: false } };
export const limits = { left: { min: 188, max: 380, rail: 56 }, right: { min: 232, max: 420, rail: 44 } };

function fitLayout(layout: Layout, frameWidth: number): Layout {
  if (frameWidth < 860) return layout;
  const result = structuredClone(layout);
  const occupied = (side: Side) => result[side].closed ? limits[side].rail : result[side].width;
  let excess = occupied("left") + occupied("right") + 392 - frameWidth;
  for (const side of ["left", "right"] as const) {
    if (result[side].closed || excess <= 0) continue;
    const reduction = Math.min(excess, result[side].width - limits[side].min);
    result[side].width -= reduction; excess -= reduction;
  }
  return result;
}

function readLayouts(): Record<Variant, Layout> {
  const result = { a: structuredClone(defaults), b: structuredClone(defaults), c: structuredClone(defaults) };
  try {
    const saved: unknown = JSON.parse(localStorage.getItem(storageKey) || "null");
    if (!saved || typeof saved !== "object") return result;
    for (const variant of ["a", "b", "c"] as const) {
      if (!(variant in saved)) continue;
      const layout: unknown = (saved as Record<string, unknown>)[variant];
      if (!layout || typeof layout !== "object") continue;
      for (const side of ["left", "right"] as const) {
        const value: unknown = (layout as Record<string, unknown>)[side];
        if (!value || typeof value !== "object" || !("width" in value) || !("closed" in value)) continue;
        if (typeof value.width !== "number" || !Number.isFinite(value.width) || typeof value.closed !== "boolean") continue;
        result[variant][side] = { width: Math.min(limits[side].max, Math.max(limits[side].min, value.width)), closed: value.closed };
      }
    }
  } catch { /* A demo remains usable when storage is unavailable. */ }
  return result;
}

export function useLayout(variant: Variant) {
  const [layouts, setLayouts] = useState(readLayouts);
  const frame = useRef<HTMLDivElement>(null);
  const [frameWidth, setFrameWidth] = useState(window.innerWidth - 32);
  const [dragging, setDragging] = useState<Side>();
  const drag = useRef<{ x: number; width: number; side: Side; pointer: number } | undefined>(undefined);
  const layout = layouts[variant];
  const compact = frameWidth < 860;
  const wasCompact = useRef(compact);

  useEffect(() => {
    try { localStorage.setItem(storageKey, JSON.stringify(layouts)); } catch { /* Optional persistence. */ }
  }, [layouts]);

  useEffect(() => {
    if (!frame.current) return;
    const observer = new ResizeObserver(entries => setFrameWidth(entries[0].contentRect.width));
    observer.observe(frame.current);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    setLayouts(previous => Object.fromEntries(Object.entries(previous).map(([key, value]) =>
      [key, fitLayout(value, frameWidth)])) as Record<Variant, Layout>);
  }, [frameWidth]);

  useEffect(() => {
    if (compact && !wasCompact.current) {
      setLayouts(previous => Object.fromEntries(Object.entries(previous).map(([key, value]) => [key,
        { left: { ...value.left, closed: true }, right: { ...value.right, closed: true } }])) as Record<Variant, Layout>);
    }
    wasCompact.current = compact;
  }, [compact]);

  useEffect(() => {
    if (window.innerWidth < 892) setLayouts(previous => Object.fromEntries(Object.entries(previous).map(([key, value]) => [key,
      { left: { ...value.left, closed: true }, right: { ...value.right, closed: true } }])) as Record<Variant, Layout>);
  }, []);

  function update(side: Side, next: Partial<Panel>) {
    setLayouts(previous => {
      const current = previous[variant];
      const other: Side = side === "left" ? "right" : "left";
      const otherWidth = current[other].closed ? limits[other].rail : current[other].width;
      const maximum = compact ? limits[side].max : Math.max(limits[side].min, Math.min(limits[side].max, frameWidth - otherWidth - 360 - 32));
      const width = Math.round(Math.max(limits[side].min, Math.min(maximum, next.width ?? current[side].width)));
      const result = { ...current, [side]: { ...current[side], ...next, width } };
      if (compact && next.closed === false) result[other] = { ...result[other], closed: true };
      return { ...previous, [variant]: fitLayout(result, frameWidth) };
    });
  }

  function toggle(side: Side) { update(side, { closed: !layout[side].closed }); }
  function resetSide(side: Side) { update(side, { width: defaults[side].width, closed: false }); }
  function reset() {
    const next = structuredClone(defaults);
    if (compact) { next.left.closed = true; next.right.closed = true; }
    setLayouts(previous => ({ ...previous, [variant]: fitLayout(next, frameWidth) }));
  }
  function focus() { setLayouts(previous => ({ ...previous, [variant]: { left: { ...layout.left, closed: true }, right: { ...layout.right, closed: true } } })); }

  function start(event: PointerEvent<HTMLDivElement>, side: Side) {
    if (event.button !== 0 || compact) return;
    event.preventDefault(); event.currentTarget.focus({ preventScroll: true });
    event.currentTarget.setPointerCapture(event.pointerId);
    drag.current = { x: event.clientX, width: layout[side].width, side, pointer: event.pointerId };
    setDragging(side);
    if (layout[side].closed) update(side, { closed: false });
  }

  function move(event: PointerEvent<HTMLDivElement>) {
    const current = drag.current;
    if (!current || current.pointer !== event.pointerId) return;
    update(current.side, { width: current.width + (event.clientX - current.x) * (current.side === "left" ? 1 : -1) });
  }

  function end(event: PointerEvent<HTMLDivElement>) {
    if (drag.current?.pointer !== event.pointerId) return;
    drag.current = undefined; setDragging(undefined);
    if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
  }

  function key(event: KeyboardEvent<HTMLDivElement>, side: Side) {
    if (event.key === "Escape" && drag.current) {
      const current = drag.current; update(current.side, { width: current.width });
      drag.current = undefined; setDragging(undefined);
      if (event.currentTarget.hasPointerCapture(current.pointer)) event.currentTarget.releasePointerCapture(current.pointer);
      event.preventDefault(); return;
    }
    if (event.key === "Enter") { event.preventDefault(); toggle(side); return; }
    if (event.key === "Home" || event.key === "End") {
      event.preventDefault(); update(side, { width: limits[side][event.key === "Home" ? "min" : "max"], closed: false }); return;
    }
    if (!event.key.startsWith("Arrow") || !["ArrowLeft", "ArrowRight"].includes(event.key)) return;
    event.preventDefault();
    const direction = (event.key === "ArrowRight" ? 1 : -1) * (side === "left" ? 1 : -1);
    update(side, { width: layout[side].width + direction * (event.shiftKey ? 32 : 8), closed: false });
  }

  return { frame, layout, compact, dragging, toggle, reset, resetSide, focus,
    separator: (side: Side) => ({ onPointerDown: (event: PointerEvent<HTMLDivElement>) => start(event, side),
      onPointerMove: move, onPointerUp: end, onPointerCancel: end,
      onLostPointerCapture: () => { drag.current = undefined; setDragging(undefined); },
      onDoubleClick: () => resetSide(side), onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => key(event, side) }) };
}
