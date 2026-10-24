import { useLayoutEffect, useRef, useState } from "react";

export function SlidingActivityText({ text, activity, tool = false }: { text: string; activity: string; tool?: boolean }) {
  const viewport=useRef<HTMLSpanElement>(null),content=useRef<HTMLSpanElement>(null),entrance=useRef<HTMLSpanElement>(null);
  const runtime=useRef({current:0,target:0,frame:0,previous:0});
  const measure=useRef<()=>void>(()=>{});
  const [overflow,setOverflow]=useState(false);
  useLayoutEffect(()=>{
    const box=viewport.current,line=content.current,view=box?.ownerDocument.defaultView;
    if(!box||!line||!view)return;
    const state=runtime.current;
    const reduced=typeof view.matchMedia==="function"?view.matchMedia("(prefers-reduced-motion: reduce)"):undefined;
    const request=(callback:FrameRequestCallback)=>typeof view.requestAnimationFrame==="function"?view.requestAnimationFrame(callback):requestAnimationFrame(callback);
    const cancel=(handle:number)=>typeof view.cancelAnimationFrame==="function"?view.cancelAnimationFrame(handle):cancelAnimationFrame(handle);
    const tick=()=>{
      const now=view.performance.now(),delta=state.previous?Math.min(64,now-state.previous):16;state.previous=now;
      state.current+=(state.target-state.current)*(1-Math.exp(-delta/55));
      if(Math.abs(state.target-state.current)<.1)state.current=state.target;
      line.style.transform=`translate3d(${state.current}px,0,0)`;
      state.frame=state.current===state.target?0:request(tick);if(!state.frame)state.previous=0;
    };
    const update=()=>{
      const extra=Math.max(0,line.scrollWidth-box.clientWidth);setOverflow(extra>1);
      state.target=tool?0:-extra;
      if(reduced?.matches){if(state.frame)cancel(state.frame);state.frame=0;state.previous=0;state.current=state.target;line.style.transform=`translate3d(${state.current}px,0,0)`;}
      else if(!state.frame)state.frame=request(tick);
    };
    const Observer=view.ResizeObserver;
    const observer=typeof Observer==="function"?new Observer(update):undefined;
    observer?.observe(box);observer?.observe(line);measure.current=update;reduced?.addEventListener("change",update);update();
    return()=>{observer?.disconnect();reduced?.removeEventListener("change",update);if(state.frame)cancel(state.frame);state.frame=0;state.previous=0;measure.current=()=>{};};
  },[tool]);
  useLayoutEffect(()=>{
    const state=runtime.current;state.current=0;state.target=0;
    if(content.current)content.current.style.transform="translate3d(0,0,0)";
    const element=entrance.current,view=element?.ownerDocument.defaultView;
    let animation:Animation|undefined;
    const preference=view?.matchMedia?.("(prefers-reduced-motion: reduce)");
    if(element&&typeof element.animate==="function"&&!preference?.matches)
      animation=element.animate([{transform:"translateY(6px)"},{transform:"translateY(0)"}],{duration:160,easing:"cubic-bezier(.2,.8,.2,1)"});
    const reduce=()=>{if(preference?.matches)animation?.cancel();};
    preference?.addEventListener("change",reduce);measure.current();
    return()=>{animation?.cancel();preference?.removeEventListener("change",reduce);};
  },[activity]);
  useLayoutEffect(()=>{measure.current();},[text]);
  return <span ref={viewport} className="delegation-tail" data-overflow={overflow} data-tool={tool} title={text}><span ref={entrance} className="delegation-tail-entrance"><span ref={content} className="delegation-tail-content">{text.replace(/\s+/g," ").trim()}</span></span></span>;
}
