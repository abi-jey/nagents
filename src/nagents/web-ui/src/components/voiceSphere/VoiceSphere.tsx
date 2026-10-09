import { forwardRef, useEffect, useImperativeHandle, useRef, type CSSProperties, type PointerEvent } from "react";
import { useVoiceSphere, type VoiceSphereController, type VoiceSphereHandle, type VoiceSphereOptions } from "./useVoiceSphere.js";

export interface VoiceSphereCanvasProps {
  controller: VoiceSphereController;
  compact?: boolean;
  className?: string;
  style?: CSSProperties;
  label?: string;
  onReady?: (available: boolean) => void;
}

/** Multiple views may share one controller, keeping their simulation in sync. */
export function VoiceSphereCanvas({ controller, compact = false, className, style, label, onReady }: VoiceSphereCanvasProps) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const drag = useRef<{ x: number; y: number; moved: boolean } | undefined>(undefined);
  const attach = controller.attach;
  useEffect(() => {
    const element = canvas.current; if (!element) return;
    return attach(element, compact, onReady);
  }, [attach, compact, onReady]);
  const zoomBy = controller.zoomBy;
  useEffect(() => {
    const element = canvas.current; if (!element || compact) return;
    const wheel = (event: WheelEvent) => { if (!event.ctrlKey) { event.preventDefault(); zoomBy(-event.deltaY * .001); } };
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
  }, [compact, zoomBy]);
  const local = (event: PointerEvent<HTMLCanvasElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    return { x: event.clientX - box.left, y: event.clientY - box.top };
  };
  const end = () => { drag.current = undefined; controller.setDragging(false); };
  return <canvas ref={canvas} className={className} style={style} role="img"
    aria-label={label ?? (compact ? "Network sphere at 80 pixels" : "Interactive connected network sphere. Drag to rotate; click a node to send a pulse.")}
    data-mode={controller.snapshot.mode} data-nodes={controller.snapshot.nodes} data-outer-scale={controller.snapshot.outerScale}
    data-inner-scale={controller.snapshot.innerScale} data-ambient-packets={controller.snapshot.ambientPackets}
    data-voice-packets={controller.snapshot.voicePackets} data-shock={controller.snapshot.shock}
    onPointerDown={compact ? undefined : event => {
      const point = local(event); drag.current = { ...point, moved: false };
      controller.setDragging(true); event.currentTarget.setPointerCapture(event.pointerId);
    }}
    onPointerMove={compact ? undefined : event => {
      const point = local(event), current = drag.current;
      if (current) {
        const dx = point.x - current.x, dy = point.y - current.y;
        if (current.moved || Math.abs(dx) + Math.abs(dy) > 3) {
          current.moved = true; controller.rotate(dx * .009, dy * .007); current.x = point.x; current.y = point.y;
        }
      } else if (event.pointerType !== "touch") controller.pointer(point.x, point.y);
    }}
    onPointerUp={compact ? undefined : event => {
      if (drag.current && !drag.current.moved) { const point = local(event); controller.selectAt(point.x, point.y); }
      end();
    }}
    onPointerCancel={compact ? undefined : end}
    onLostPointerCapture={compact ? undefined : end}
    onPointerLeave={compact ? undefined : () => { if (!drag.current) controller.clearPointer(); }}
  />;
}

export interface VoiceSphereProps extends VoiceSphereOptions {
  compact?: boolean;
  className?: string;
  style?: CSSProperties;
  label?: string;
}

/** Drop-in view. It never requests media permission or plays audio. */
export const VoiceSphere = forwardRef<VoiceSphereHandle, VoiceSphereProps>(function VoiceSphere({ compact, className, style, label, ...options }, ref) {
  const controller = useVoiceSphere(options);
  useImperativeHandle(ref, () => ({ dispatch: controller.dispatch, rotate: controller.rotate, nextNode: controller.nextNode, zoomBy: controller.zoomBy, getSnapshot: controller.getSnapshot }), [controller.dispatch, controller.rotate, controller.nextNode, controller.zoomBy, controller.getSnapshot]);
  return <VoiceSphereCanvas controller={controller} compact={compact} className={className} style={{ display: "block", width: compact ? 80 : 320, height: compact ? 80 : 320, ...style }} label={label} />;
});
