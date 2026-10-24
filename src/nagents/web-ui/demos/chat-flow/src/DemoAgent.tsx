import { useId, useState } from "react";
import { Conversation } from "../../../src/features/chat/Conversation.js";
import type { TaskThread } from "../../../src/features/chat/transcript.js";
import type { ResponseDetailsProps } from "../../../src/features/chat/ResponseDetails.js";
import type { ChatAppearance } from "../../../src/features/settings/appearance.js";
import type { ReactNode } from "react";
import { Icon } from "../../../src/components/Icon.js";
import { demoAgents } from "./engine.js";

export function DemoAgent({ thread, appearance, renderReasoning }: { thread: TaskThread; appearance: ChatAppearance; renderReasoning: (props: ResponseDetailsProps) => ReactNode }) {
  const id = useId();
  const [choice, setChoice] = useState<boolean>();
  const profile = demoAgents[thread.id as keyof typeof demoAgents];
  const open = choice ?? appearance.activity.agents.details === "expanded";
  const working = thread.state === "Running";
  const entries = thread.items.flatMap(item => item.kind === "entry" && item.entry.kind !== "task"
    ? [{ ...item.entry, taskId: "", parentTaskId: "", taskName: "" }] : []);
  return <section className="demo-agent" data-state={thread.state} id={`task-${thread.id}`} data-record-key={`task:${thread.id}`}>
    <button className="demo-agent-toggle" type="button" aria-expanded={open} aria-controls={id}
      aria-label={`${open ? "Hide" : "Inspect"} ${profile.name}, spawned by ${profile.parent}, ${working ? "working" : "result returned"}`} onClick={() => setChoice(!open)}>
      <Icon name="team" size={12} /><strong className="demo-agent-name">{profile.name}</strong><span className="demo-agent-model">{profile.provider} · {profile.model}</span>
      <span className="sr-only" role="status">{profile.name}: {working ? "working" : "result returned"}</span>
    </button>
    <div id={id} hidden={!open} className="demo-agent-body">
      <p className="demo-agent-assignment">{profile.prompt}</p>
      {entries.length ? <Conversation entries={entries} agentName={profile.name} parentAgentName={profile.parent} sessionId={thread.id} appearance={appearance}
        demo={false} canSubmit={false} submit={() => {}} renderReasoning={renderReasoning} /> : <p className="demo-agent-assignment">Waiting for its first message…</p>}
    </div>
  </section>;
}
