import { Icon } from "../components/Icon.js";
import { panelLimits, type PanelSide, type usePanelLayout } from "./usePanelLayout.js";

export function PanelDivider({ side, collapsed, setCollapsed, layout }: {
  side: PanelSide; collapsed: boolean; setCollapsed: (value: boolean) => void; layout: ReturnType<typeof usePanelLayout>;
}) {
  const name = side === "sessions" ? "sessions sidebar" : "team panel";
  const label = `${collapsed ? "Expand" : "Collapse"} ${name}`;
  const toggle = () => { layout.finish(); setCollapsed(!collapsed); };
  return <div className={`panel-divider panel-divider-${side}`} data-collapsed={collapsed}>
    <div className="panel-resize-target" role="separator" tabIndex={0} aria-label={`Resize ${name}`}
      aria-orientation="vertical" aria-controls={side === "sessions" ? "session-navigation" : collapsed ? "team-navigation-collapsed" : "team-navigation"}
      aria-valuemin={collapsed ? panelLimits[side].rail : panelLimits[side].min} aria-valuemax={layout.maximum(side)}
      aria-valuenow={layout.effective[side]} aria-valuetext={collapsed ? "Collapsed" : `${layout.effective[side]} pixels`}
      title="Drag to resize. Double-click to reset. Arrow keys resize; Enter collapses or expands."
      onPointerDown={event => layout.start(event, side, collapsed, setCollapsed)} onPointerMove={layout.move}
      onPointerUp={event => layout.end(event)} onPointerCancel={event => layout.end(event, true)} onLostPointerCapture={layout.finish}
      onDoubleClick={() => { layout.reset(side); if (collapsed) setCollapsed(false); }}
      onKeyDown={event => {
        if (event.key === "Escape") { layout.cancel(); event.preventDefault(); return; }
        if (event.key === "Enter") { toggle(); event.preventDefault(); return; }
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        const direction = (event.key === "ArrowRight" ? 1 : -1) * (side === "sessions" ? 1 : -1);
        const width = event.key === "Home" ? panelLimits[side].min : event.key === "End" ? layout.maximum(side)
          : (collapsed ? layout.preferred[side] : layout.effective[side]) + direction * (event.shiftKey ? 32 : 8);
        layout.resize(side, width); if (collapsed) setCollapsed(false);
      }}><span className="panel-divider-line" aria-hidden="true" /></div>
    <button type="button" className="panel-notch" aria-label={label} title={label} aria-expanded={!collapsed}
      aria-controls={side === "sessions" ? "session-navigation" : collapsed ? "team-navigation-collapsed" : "team-navigation"}
      onClick={toggle}><Icon name="chevron" size={10} /></button>
  </div>;
}
