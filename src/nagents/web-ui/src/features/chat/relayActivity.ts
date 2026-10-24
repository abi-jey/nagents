import type { DelegationPresentation } from "./delegationFlow.js";

export type RelayState = "appearing" | "starting" | "ready" | "running" | "reasoning" | "tool" | "waiting" | "writing" | "done" | "failed" | "cancelled";
type Point = readonly [number, number];
type Weights = Record<RelayState, number>;
const states: readonly RelayState[] = ["appearing", "starting", "ready", "running", "reasoning", "tool", "waiting", "writing", "done", "failed", "cancelled"];
const layouts: Record<RelayState, readonly Point[]> = {
  appearing: [[15.4,16],[16,15.4],[16.6,16],[16,16.6]],
  starting: [[5,16],[16,5],[27,16],[16,27]],
  ready: [[12,16],[16,12],[20,16],[16,20]],
  running: [[5,17],[15,5],[27,15],[17,27]],
  reasoning: [[5,17],[15,5],[27,15],[17,27]],
  tool: [[4,16],[12,10],[20,16],[28,16]],
  waiting: [[6,16],[12,11],[25,16],[19,22]],
  writing: [[4,17],[13,8],[25,12],[28,23]],
  done: [[7,16],[13,22],[20,14],[27,6]],
  failed: [[5,16],[12,10],[24,12],[26,23]],
  cancelled: [[9,16],[14,16],[19,16],[24,16]],
};
const edges: readonly (readonly [number,number])[] = [[0,1],[1,2],[2,3],[3,0],[0,2]];
const oneHot = (target: RelayState): Weights => Object.fromEntries(states.map(state => [state,state===target?1:0])) as Weights;
const ease = (value: number): number => value<.5?4*value**3:1-(-2*value+2)**3/2;
const terminal = (state: RelayState): boolean => ["done","failed","cancelled","ready"].includes(state);

export function delegationRelayState(item: DelegationPresentation): RelayState {
  if (["done","failed","cancelled","ready"].includes(item.phase)) return item.phase;
  if (item.phase === "waiting" || item.latest?.kind === "tool" && ["Waiting for approval","Approval required"].includes(item.latest.state || "")) return "waiting";
  if (!item.taskId) {
    // A saved/returned acknowledgement without a host task cannot establish an
    // active spawn. Keep its marker settled until runtime identity arrives.
    const call=item.sources.find(source=>source.kind==="tool");
    return ["Recorded result","Completed"].includes(call?.state || "")?"ready":"starting";
  }
  if (item.latest?.kind === "tool") return "tool";
  if (item.latest?.kind === "assistant") {
    if (item.latest.text) return "writing";
    if (item.latest.responseMetadata?.summary || item.latest.responseMetadata?.status === "thinking") return "reasoning";
  }
  return "running";
}

export function relayAppears(item: DelegationPresentation): boolean {
  return item.kind === "start" && !terminal(delegationRelayState(item)) &&
    item.sources.some(source => source.kind === "tool" && source.state !== "Recorded result" && !source.recorded);
}

export type RelayFrame = {
  points: {x:number;y:number;radius:number}[];
  edges: {path:string;opacity:number;dash:string}[];
  packets: {x:number;y:number;opacity:number}[];
  animate: boolean;
};

/** Bounded visual interpolation; it never changes task execution state. */
export class RelayMotion {
  private weights: Weights;
  private from: Weights;
  private target: RelayState;
  private began = 0;
  private duration = 520;
  private phase = 0;
  private previous = 0;
  private settled = true;

  constructor(initial: RelayState) { this.weights=oneHot(initial);this.from={...this.weights};this.target=initial; }

  transition(target: RelayState, now: number): void {
    if (target===this.target) return;
    this.sample(now,false);
    this.from={...this.weights};this.target=target;this.began=now;
    this.duration=target==="starting"||target==="appearing"?420:520;
    this.settled=false;
  }

  sample(now: number, reduced: boolean): RelayFrame {
    const progress=reduced?1:this.settled?1:Math.min(1,Math.max(0,(now-this.began)/this.duration));
    const amount=ease(progress);
    if (!this.settled) this.weights=Object.fromEntries(states.map(state=>[state,this.from[state]+((state===this.target?1:0)-this.from[state])*amount])) as Weights;
    if (reduced) this.weights=oneHot(this.target);
    this.settled=progress>=1;
    const w=this.weights, active=w.starting+w.running+w.reasoning+w.tool+w.writing;
    const delta=this.previous?Math.min(64,Math.max(0,now-this.previous)):16;this.previous=now;
    if(!reduced)this.phase+=delta/1000*(.3+w.tool*.27+w.writing*.105);
    const points=layouts.ready.map((_,index)=>{
      let x=0,y=0;for(const state of states){x+=layouts[state][index][0]*w[state];y+=layouts[state][index][1]*w[state];}
      const drift=reduced?0:.35*(w.reasoning+w.writing);
      x+=Math.sin(now/1000*1.4+index*1.7)*drift;y+=Math.cos(now/1000*1.2+index*1.4)*drift;
      const impact=reduced?0:Math.max(0,Math.sin((this.phase-index/4)*Math.PI*2))**16*.38*active;
      const hold=reduced?0:w.waiting*.13*(1+Math.sin(now/1000*2.2+index*.6));
      return {x,y,radius:1.9*(1-w.done*.3)*(1-w.appearing*.8)+impact+hold};
    });
    const connections=edges.map(([from,to],index)=>({
      path:`M${points[from].x} ${points[from].y}L${points[to].x} ${points[to].y}`,
      opacity:(index<3?1:1-w.done)*(.3+w.done*.55)*(1-w.appearing*.65),
      dash:`${32*(1-w.failed)+3*w.failed} ${2*w.failed}`,
    }));
    const packets=[0,.37,.71].map(offset=>{
      const position=((this.phase+offset)%1+1)%1*edges.length,index=Math.floor(position),t=position%1;
      const [from,to]=edges[Math.min(edges.length-1,index)],a=points[from],b=points[to];
      return {x:a.x+(b.x-a.x)*t,y:a.y+(b.y-a.y)*t,opacity:reduced?0:active*.95*Math.sin(Math.PI*t)**.35};
    });
    return {points,edges:connections,packets,animate:!reduced&&(!this.settled||active>.001||w.waiting>.001)};
  }
}
