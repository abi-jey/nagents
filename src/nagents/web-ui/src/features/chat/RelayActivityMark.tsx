import { memo, useEffect, useLayoutEffect, useRef, useState } from "react";
import { RelayMotion, type RelayState } from "./relayActivity.js";

export const RelayActivityMark = memo(function RelayActivityMark({ state, appear = false }: { state: RelayState; appear?: boolean }) {
  const [arriving,setArriving]=useState(appear);
  const terminal=["done","failed","cancelled","ready"].includes(state);
  const visual=arriving&&!terminal?"appearing":state;
  const svg=useRef<SVGSVGElement>(null);
  const [motion]=useState(()=>new RelayMotion(visual));
  const draw=useRef<()=>void>(()=>{});
  useEffect(()=>{
    if(!arriving)return;
    const view=svg.current?.ownerDocument.defaultView;
    const preference=view?.matchMedia?.("(prefers-reduced-motion: reduce)");
    if(preference?.matches){setArriving(false);return;}
    const reduce=()=>{if(preference?.matches)setArriving(false);};
    preference?.addEventListener("change",reduce);
    const timer=setTimeout(()=>setArriving(false),180);
    return()=>{clearTimeout(timer);preference?.removeEventListener("change",reduce);};
  },[arriving]);
  useLayoutEffect(()=>{
    const element=svg.current,view=element?.ownerDocument.defaultView;
    if(!element||!view)return;
    const nodes=[...element.querySelectorAll<SVGCircleElement>(".relay-nodes circle")];
    const edges=[...element.querySelectorAll<SVGPathElement>(".relay-edges path")];
    const packets=[...element.querySelectorAll<SVGCircleElement>(".relay-packets circle")];
    const preference=typeof view.matchMedia==="function"?view.matchMedia("(prefers-reduced-motion: reduce)"):undefined;
    const request=(callback:FrameRequestCallback)=>typeof view.requestAnimationFrame==="function"?view.requestAnimationFrame(callback):requestAnimationFrame(callback);
    const cancel=(handle:number)=>typeof view.cancelAnimationFrame==="function"?view.cancelAnimationFrame(handle):cancelAnimationFrame(handle);
    let frame=0,disposed=false;
    const render=()=>{
      frame=0;if(disposed)return;
      const value=motion.sample(view.performance.now(),preference?.matches===true);
      value.points.forEach((point,index)=>{nodes[index].setAttribute("cx",String(point.x));nodes[index].setAttribute("cy",String(point.y));nodes[index].setAttribute("r",String(point.radius));});
      value.edges.forEach((edge,index)=>{edges[index].setAttribute("d",edge.path);edges[index].setAttribute("opacity",String(edge.opacity));edges[index].setAttribute("stroke-dasharray",edge.dash);});
      value.packets.forEach((packet,index)=>{packets[index].setAttribute("cx",String(packet.x));packets[index].setAttribute("cy",String(packet.y));packets[index].setAttribute("opacity",String(packet.opacity));});
      if(value.animate)frame=request(render);
    };
    draw.current=()=>{if(frame)cancel(frame);render();};
    preference?.addEventListener("change",draw.current);draw.current();
    return()=>{disposed=true;if(frame)cancel(frame);preference?.removeEventListener("change",draw.current);draw.current=()=>{};};
  },[]);
  useLayoutEffect(()=>{motion.transition(visual,svg.current?.ownerDocument.defaultView?.performance.now()||0);draw.current();},[visual]);
  return <span className="delegation-mark relay-activity-mark" data-state={visual} aria-hidden="true"><svg ref={svg} viewBox="0 0 32 32" fill="none" stroke="currentColor" strokeWidth="1.15" strokeLinecap="round" strokeLinejoin="round">
    <g className="relay-edges">{[0,1,2,3,4].map(index=><path key={index}/>)}</g>
    <g className="relay-packets" fill="currentColor" stroke="none">{[0,1,2].map(index=><circle key={index} r=".85"/>)}</g>
    <g className="relay-nodes" fill="currentColor" stroke="none">{[0,1,2,3].map(index=><circle key={index}/>)}</g>
  </svg></span>;
});
