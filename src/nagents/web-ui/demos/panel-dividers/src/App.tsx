import { useState, type CSSProperties, type ReactNode } from "react";
import { Icon, type IconName } from "../../../src/components/Icon";
import { limits, useLayout, type Side, type Variant } from "./layout";

const studies = {
  a: { title: "Compact", detail: "A 14 × 34 px soft notch. A little taller, with short rounded shoulders." },
  b: { title: "Balanced", detail: "A 13 × 42 px notch with a tighter curve at the corners." },
  c: { title: "Slender", detail: "A 12 × 50 px soft notch. Tall and narrow, with longer curved shoulders." },
};
type LayoutController = ReturnType<typeof useLayout>;

function PanelIcon({ side, closed = false }: { side: Side; closed?: boolean }) {
  return <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="2.5" y="3" width="15" height="14" rx="2" /><path d={side === "left" ? "M7 3v14" : "M13 3v14"} />
    <path d={side === "left" ? closed ? "m10 8 2 2-2 2" : "m12 8-2 2 2 2" : closed ? "m10 8-2 2 2 2" : "m8 8 2 2-2 2"} />
  </svg>;
}

function ToolButton({ icon, label, onClick, children, className = "" }: { icon: IconName; label: string; onClick?: () => void; children?: ReactNode; className?: string }) {
  return <button type="button" className={`tool-button ${className}`} title={label} aria-label={label} onClick={onClick}><Icon name={icon} size={17} />{children}</button>;
}

function Divider({ side, controller }: { side: Side; controller: LayoutController }) {
  const panel = controller.layout[side]; const name = side === "left" ? "sessions" : "team";
  const closed = panel.closed; const action = `${closed ? "Expand" : "Collapse"} ${name} panel`;
  return <div className={`divider divider-${side}`} data-closed={closed}>
    <div role="separator" tabIndex={controller.compact ? -1 : 0} className="resize-edge"
      aria-label={`Resize ${name} panel`} aria-orientation="vertical" aria-controls={`${name}-panel`}
      aria-valuemin={closed ? limits[side].rail : limits[side].min} aria-valuemax={limits[side].max}
      aria-valuenow={closed ? limits[side].rail : panel.width} aria-valuetext={closed ? "Collapsed" : `${panel.width} pixels`}
      aria-describedby="resize-help" {...controller.separator(side)}>
      <span className="edge-line" />
      <span className="resize-value" aria-hidden="true">{panel.width}<span>px</span></span>
    </div>
    <button type="button" className="edge-collapse" aria-label={action} title={action}
      aria-expanded={!closed} aria-controls={`${name}-panel`} onClick={() => controller.toggle(side)}>
      <Icon name="chevron" size={13} /><span className="sr-only">{action}</span>
    </button>
  </div>;
}

function PanelToggle({ side, controller }: { side: Side; controller: LayoutController }) {
  const closed = controller.layout[side].closed;
  const label = `${closed ? "Expand" : "Collapse"} ${side === "left" ? "sessions" : "team"} panel`;
  return <button type="button" className="panel-toggle" aria-label={label} title={label} aria-expanded={!closed}
    aria-controls={`${side === "left" ? "sessions" : "team"}-panel`} onClick={() => controller.toggle(side)}>
    <PanelIcon side={side} closed={closed} />
  </button>;
}

function Sidebar({ controller, scrollTest }: { controller: LayoutController; scrollTest: boolean }) {
  const closed = controller.layout.left.closed;
  const [active, setActive] = useState(0);
  const sessions = ["Upload preview improvements", "A quieter voice interface", "Review the agent handoff", "Workspace setup", "Mobile navigation",
    ...(scrollTest ? Array.from({ length: 18 }, (_, index) => `Workspace notes ${index + 1}`) : [])];
  const [section, setSection] = useState("Sessions");
  return <aside id="sessions-panel" className="sessions-panel panel" data-closed={closed} aria-label="Sessions">
    <div className="panel-inner" aria-hidden={closed} inert={closed}>
      <header className="side-heading"><div className="brand"><span className="brand-mark">n</span><span>ngn<small>Workspace</small></span></div>
        {controller.compact && <PanelToggle side="left" controller={controller} />}
      </header>
      <button type="button" className="new-chat" onClick={() => { setActive(-1); setSection("New session"); }}><Icon name="plus" size={17} />New session</button>
      <div className="nav-label"><span>{section}</span><span>{sessions.length}</span></div>
      <label className="session-search"><Icon name="search" size={14} /><input placeholder="Search chats" aria-label="Search demo chats" onChange={event => {
        const query = event.target.value.toLowerCase();
        document.querySelectorAll<HTMLElement>(".session-item").forEach(item => { item.hidden = !item.dataset.title?.includes(query); });
      }} /></label>
      <nav className="session-items" aria-label="Demo chat navigation">{sessions.map((title, index) => <button type="button" key={title}
        className={`session-item ${index === active ? "selected" : ""}`} data-title={title.toLowerCase()} aria-current={index === active ? "page" : undefined}
        onClick={() => { setActive(index); setSection("Sessions"); }}><span>{title}</span><small>{index < 2 ? "Today" : "Yesterday"}</small><Icon name="more" size={15} /></button>)}</nav>
      <div className="sidebar-spacer" />
      <nav className="side-utilities" aria-label="Workspace actions">{([['trash', 'Trash'], ['settings', 'Settings'], ['channels', 'Channels'], ['tools', 'Tools']] as const).map(([icon, label]) => <button key={label} type="button" onClick={() => setSection(label)}><Icon name={icon} size={16} />{label}</button>)}</nav>
      <button className="workspace-location" type="button" onClick={() => setSection("Workspace")}><Icon name="folder" size={16} /><span>workspace<small>/home/workspace</small></span><Icon name="chevron" size={12} /></button>
    </div>
    <div className="collapsed-rail left-rail" aria-hidden={!closed} inert={!closed}>
      <button className="brand-mark" type="button" aria-label="Expand sessions panel" onClick={() => controller.toggle("left")}>n</button>
      <ToolButton icon="plus" label="New demo session" onClick={() => { controller.toggle("left"); setActive(-1); setSection("New session"); }} />
      <ToolButton icon="chat" label="Expand sessions panel" onClick={() => controller.toggle("left")} />
      <div className="sidebar-spacer" /><PanelToggle side="left" controller={controller} />
      <ToolButton icon="settings" label="Show workspace actions" onClick={() => { controller.toggle("left"); setSection("Settings"); }} />
    </div>
  </aside>;
}

const agents = [
  { id: "assistant", profile: "assistant", title: "", status: "Working", model: "gpt-6.1-sol", context: "32.4k", percent: 6.48 },
  { id: "explorer", profile: "mini", title: "gentle cedar", status: "Working", model: "gpt-6-luna", context: "8.1k", percent: 1.62 },
  { id: "reviewer", profile: "mini", title: "swift finch", status: "Done", model: "gpt-6-luna", context: "6.2k", percent: 1.24 },
];

function Team({ controller, scrollTest }: { controller: LayoutController; scrollTest: boolean }) {
  const [selected, setSelected] = useState("assistant"); const closed = controller.layout.right.closed;
  const visibleAgents = [...agents, ...(scrollTest ? Array.from({ length: 14 }, (_, index) => ({ id: `review-${index}`, profile: "mini", title: `review ${index + 1}`, status: "Done", model: "gpt-6-luna", context: "4.1k", percent: .82 })) : [])];
  return <aside className="team-panel panel" data-closed={closed} id="team-panel" aria-label="Team">
    <div className="panel-inner" aria-hidden={closed} inert={closed}>
      <header className="team-heading"><span><Icon name="team" size={17} /><strong>Team</strong><small>{visibleAgents.length}</small></span>
        {controller.compact && <PanelToggle side="right" controller={controller} />}</header>
      <p className="team-capacity">2/3 slots <span>·</span> 2/8 executions</p>
      <div className="agent-tree">{visibleAgents.map((agent, index) => <div key={agent.id} className={`agent-node ${index ? "child-node" : ""}`}>
        <button type="button" className={`agent-row ${selected === agent.id ? "is-selected" : ""}`} onClick={() => setSelected(agent.id)} aria-pressed={selected === agent.id}>
          <span className="agent-identity"><strong>{agent.profile}</strong>{agent.title && <span>{agent.title}</span>}<span className={`agent-state ${agent.status === "Done" ? "done" : "working"}`}><i />{agent.status}</span></span>
          <span className="agent-model"><img src="/assets/provider-logos/openai.svg" alt="" width="14" height="14" />openai · {agent.model}</span>
          <span className="agent-context">{agent.context} / 500k</span><span className="context-meter"><i style={{ width: `${agent.percent}%` }} /><b /></span>
        </button></div>)}</div>
      <details className="agent-definitions"><summary>Agent definitions <span>2</span></summary><button type="button" onClick={() => setSelected("assistant")}><Icon name="team" size={14} />assistant<span>1 conversation</span></button><button type="button" onClick={() => setSelected("explorer")}><Icon name="team" size={14} />mini<span>2 conversations</span></button></details>
      <div className="sidebar-spacer" /><div className="team-bottom"><span className="connection-dot" />All changes saved locally in this demo</div>
    </div>
    <div className="collapsed-rail right-rail" aria-hidden={!closed} inert={!closed}><ToolButton icon="team" label="Expand team panel" onClick={() => controller.toggle("right")} /><span className="rail-agent-count">{visibleAgents.length}</span><div className="rail-dots"><i /><i /><i /></div><div className="sidebar-spacer" /><PanelToggle side="right" controller={controller} /></div>
  </aside>;
}

function Conversation({ controller, scrollTest }: { controller: LayoutController; scrollTest: boolean }) {
  const [messages, setMessages] = useState<string[]>([]); const [draft, setDraft] = useState("");
  return <section className="chat-pane" aria-label="Demo conversation">
    <header className="chat-heading"><div className="chat-title"><span>Upload preview improvements</span><small>assistant <span>·</span> gpt-6.1-sol</small></div><button type="button" className="context-button" title="Estimated context"><span>Context</span><strong>32.4k / 500k</strong></button></header>
    <div className="conversation-scroll"><div className="conversation-content">
      <article className="message user-message"><span className="byline">You</span><p>Can you review the upload flow and check how the image preview works on a narrow screen?</p></article>
      <article className="message"><span className="byline">assistant</span><p>I’ll trace the upload flow and check the preview layout. Mini is taking a separate look at downloads and narrow screens.</p></article>
      <button type="button" className="delegation-row" onClick={() => { if (controller.layout.right.closed) controller.toggle("right"); }}><span className="network-mark" aria-hidden="true"><i /><i /><i /><i /></span><strong>mini</strong><span className="sliding-detail">read_file: src/features/chat/ChatMedia.tsx</span><Icon name="chevron" size={12} /></button>
      <details className="demo-tools"><summary><Icon name="file-read" size={15} /><span>Read file</span><code>ChatMedia.tsx</code><span className="tool-duration">12 ms</span></summary><pre>{`{\n  "path": "src/features/chat/ChatMedia.tsx",\n  "start_line": 1,\n  "end_line": 160\n}`}</pre></details>
      <article className="message continuation"><p>The image is already available in the conversation. I’m checking the filename, download action, and how the card fits when the side panels are open.</p><div className="preview-attachment"><div className="image-swatch"><span className="image-orbit" /><span className="image-orbit second" /><span className="image-core" /><span className="image-grain" /></div><div className="attachment-footer"><Icon name="image" size={15} /><span>network-study.png<small>1280 × 720 · PNG</small></span><ToolButton icon="download" label="Download demo image information" onClick={() => { const blob = new Blob(["Interactive layout demo. This image is a CSS illustration."], { type: "text/plain" }); const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = "demo-image-info.txt"; link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }} /></div></div></article>
      {scrollTest && Array.from({ length: 10 }, (_, index) => <article className="message" key={`scroll-${index}`}><span className="byline">assistant <small>Sample {index + 1}</small></span><p>{[
        "The filename and download action stay together. The image fits the available chat width, including when both side panels are open.",
        "Each panel has its own scroll area. You can scroll this conversation, the saved sessions, and the team independently while trying the divider controls.",
        "The collapse control keeps the previous panel width. Reopening the panel returns to that width without changing the conversation or the draft.",
      ][index % 3]}</p></article>)}
      {messages.map((message, index) => <article className="message user-message" key={index}><span className="byline">You <small>Demo</small></span><p>{message}</p></article>)}
    </div></div>
    <footer className="composer-area"><form onSubmit={event => { event.preventDefault(); if (!draft.trim()) return; setMessages(previous => [...previous, draft.trim()]); setDraft(""); }}>
      <label className="sr-only" htmlFor="demo-composer">Message ngn demo</label><textarea id="demo-composer" rows={2} placeholder="What should we work on?" value={draft} onChange={event => setDraft(event.target.value)} onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} />
      <div className="composer-bottom"><span className="composer-buttons"><label className="attach-button"><Icon name="paperclip" size={16} /><span>Attach</span><input type="file" aria-label="Choose a demo attachment" onChange={event => { const name = event.target.files?.[0]?.name; if (name) setDraft(previous => `${previous}${previous ? "\n" : ""}[${name}]`); }} /></label><button type="button" className="voice-button" onClick={() => setDraft("Try dragging either divider while this input stays in place.")}><Icon name="wave" size={16} /><span>Voice</span></button></span><button type="submit" className="send-button" disabled={!draft.trim()} aria-label="Send demo message"><svg width="17" height="17" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M10 16V4m-5 5 5-5 5 5" /></svg></button></div>
    </form><span className="composer-caption">Interactive layout demo <span>·</span> Messages stay in this browser</span></footer>
  </section>;
}

export function App() {
  const [variant, setVariant] = useState<Variant>(() => { const v = new URLSearchParams(location.search).get("v"); return v === "b" || v === "c" ? v : "a"; });
  const [light, setLight] = useState(false); const [scrollTest, setScrollTest] = useState(false); const controller = useLayout(variant); const { layout, compact } = controller;
  const widths = { "--left-width": `${layout.left.closed ? 56 : layout.left.width}px`, "--right-width": `${layout.right.closed ? 44 : layout.right.width}px`, "--left-saved": `${layout.left.width}px`, "--right-saved": `${layout.right.width}px` } as CSSProperties;
  return <div className="demo-shell" data-theme={light ? "light" : "dark"}>
    <header className="study-toolbar"><div className="study-brand"><span>ngn</span><i /><span>Soft notch refinements</span></div><nav className="study-options" aria-label="Divider designs">{(["a", "b", "c"] as const).map((option, index) => <button key={option} type="button" aria-pressed={variant === option} onClick={() => { setVariant(option); history.replaceState(null, "", `?v=${option}`); }}><b>B{index + 1}</b><span>{studies[option].title}</span></button>)}</nav><div className="study-actions"><button type="button" aria-pressed={scrollTest} onClick={() => setScrollTest(!scrollTest)} title="Fill all panels to try their scrollbars"><Icon name="file-read" size={15} /><span>Scroll test</span></button><button type="button" onClick={controller.focus} title="Collapse both side panels"><Icon name="expand" size={15} /><span>Focus chat</span></button><button type="button" onClick={controller.reset}><Icon name="compact" size={15} /><span>Reset</span></button><button type="button" aria-label={light ? "Use dark theme" : "Use light theme"} title={light ? "Use dark theme" : "Use light theme"} onClick={() => setLight(!light)}><span className="theme-icon" aria-hidden="true">◐</span></button></div></header>
    <div className="study-caption"><div><strong>{studies[variant].title}</strong><span>{studies[variant].detail}</span></div><span className="study-number">0{variant === "a" ? 1 : variant === "b" ? 2 : 3} / 03</span></div>
    <div ref={controller.frame} className={`workspace-frame soft-notch notch-${variant}${compact ? " is-compact" : ""}`} style={widths} data-dragging={controller.dragging || ""}>
      <Sidebar controller={controller} scrollTest={scrollTest} /><Divider side="left" controller={controller} />
      <Conversation controller={controller} scrollTest={scrollTest} /><Divider side="right" controller={controller} /><Team controller={controller} scrollTest={scrollTest} />
      {compact && (!layout.left.closed || !layout.right.closed) && <button type="button" className="drawer-scrim" aria-label="Close side panels" onClick={controller.focus} />}
    </div>
    <footer className="study-footer"><span id="resize-help">{compact ? "Open a panel from either rail. Widen the window to try the resize handles." : "Drag either divider · Double-click to reset its width · Arrow keys to resize · Enter to collapse"}</span><span className="layout-measurement"><span>Sessions <b>{layout.left.closed ? "closed" : `${layout.left.width}px`}</b></span><i /><span>Team <b>{layout.right.closed ? "closed" : `${layout.right.width}px`}</b></span></span></footer>
  </div>;
}
