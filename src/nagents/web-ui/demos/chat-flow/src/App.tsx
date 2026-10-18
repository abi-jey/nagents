import { useEffect, useMemo, useState } from "react";
import { Conversation } from "../../../src/features/chat/Conversation.js";
import { ResponseDetails, type ResponseDetailsProps } from "../../../src/features/chat/ResponseDetails.js";
import { Icon } from "../../../src/components/Icon.js";
import { AppearanceSettings } from "../../../src/features/settings/AppearanceSettings.js";
import { appearancePresets, saveAppearance, selectedAppearancePreset, useAppearance } from "../../../src/features/settings/appearance.js";
import { Reasoning, type Treatment } from "./Reasoning.js";
import { chapters, demoAgents, DURATION, frameAt, nextTime, timeline } from "./engine.js";
import { DemoAgent } from "./DemoAgent.js";

const treatments: { id: Treatment; name: string; detail: string }[] = [
  { id: "inline", name: "A · Inline", detail: "One quiet heading. Open the text directly in the conversation." },
  { id: "margin", name: "B · Margin", detail: "A fine vertical trace keeps the note beside the answer’s reading line." },
  { id: "excerpt", name: "C · Read first", detail: "The first summary sentence becomes the control. Open it for the rest." },
];
const stamp = (value: number) => `0:${Math.floor(value).toString().padStart(2, "0")}`;
export function App() {
  const [treatment, setTreatment] = useState<Treatment>("excerpt");
  const [time, setTime] = useState(21.6);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [settings, setSettings] = useState(false);
  const [context, setContext] = useState(false);
  const [eventList, setEventList] = useState(false);
  const [generation, setGeneration] = useState(0);
  const appearance = useAppearance();
  const detail = selectedAppearancePreset(appearance);
  const renderReasoning = (props: ResponseDetailsProps) => treatment !== "excerpt" && props.metadata && !props.hideSummary &&
    !["cancelled", "failed"].includes(props.metadata.status)
    ? <Reasoning metadata={props.metadata} open={props.open} toggle={props.toggle} /> : <ResponseDetails {...props} />;
  const frame = useMemo(() => frameAt(time), [time]);
  useEffect(() => {
    if (!playing) return;
    let previous = performance.now();
    const timer = window.setInterval(() => {
      const now = performance.now();
      const delta = (now - previous) / 1000 * speed;
      previous = now;
      setTime(value => Math.min(DURATION, value + delta));
    }, 50);
    return () => window.clearInterval(timer);
  }, [playing, speed]);
  useEffect(() => { if (time >= DURATION) setPlaying(false); }, [time]);
  function seek(value: number) { setPlaying(false); setTime(value); }
  function replay() { setGeneration(value => value + 1); setTime(0); setPlaying(true); }
  const estimate = 7140 + Math.round(frame.entries.reduce((total, entry) => total + entry.text.length + (entry.inputs?.length || 0), 0) / 4);
  const text = treatments.find(item => item.id === treatment)!;
  const queued = frame.entries.some(entry => entry.queued);
  return <div className="motion-app" data-treatment={treatment}>
    <header className="motion-header"><a className="motion-brand" href="/">ngn<span>Conversation in motion</span></a>
      <div className="treatment-switch" aria-label="Reasoning design">{treatments.map(item => <button key={item.id} type="button" aria-pressed={treatment === item.id} onClick={() => setTreatment(item.id)}>{item.name}</button>)}</div>
      <button className="motion-settings" type="button" onClick={() => setSettings(!settings)} aria-expanded={settings}><Icon name="settings" size={15} /><span>Appearance</span></button>
    </header>
    <main className={`motion-layout${settings ? " has-appearance" : ""}`}>
      {settings && <aside className="motion-appearance-panel" aria-label="Appearance settings"><div className="motion-pane-header"><strong>Appearance</strong><button type="button" aria-label="Close appearance" onClick={() => setSettings(false)}><Icon name="close" size={14} /></button></div><AppearanceSettings initiallyExpanded /></aside>}
      <section className="motion-stage" aria-label="Conversation preview">
        <header className="motion-conversation-header"><div><span className="motion-eyebrow">WORKSPACE</span><h1>Images in the conversation</h1><p>OpenAI <span>·</span> gpt-6.1-sol</p></div>
          <div className="motion-context"><button className="context-toggle" type="button" aria-expanded={context} aria-controls="demo-context" onClick={() => setContext(!context)}><span className="context-toggle-label">Context</span><span className="context-total">~{(estimate / 1000).toFixed(1)}k / 500k</span></button>
            {context && <div id="demo-context" className="motion-context-panel"><strong>Context window</strong><span>~{estimate.toLocaleString()} / 500,000 tokens</span><div className="motion-meter"><i style={{ width: `${estimate / 5000}%` }} /><b /></div><span>Compact at 350k · 70%</span><small>Illustrative values for this local replay.</small></div>}
          </div>
        </header>
        <div className="motion-detail-bar" role="group" aria-label="Conversation detail level">
          <span>Detail</span><div>{appearancePresets.map(preset => <button key={preset.id} type="button" aria-pressed={detail?.id === preset.id}
            onClick={() => saveAppearance(preset.value)}>{preset.label}</button>)}</div>
        </div>
        <div className="motion-feed">
            {!frame.entries.length && <div className="motion-empty"><span className="motion-empty-mark"><Icon name="chat" size={24} /></span><h2>Watch the conversation take shape</h2><p>A message, a thought, a tool call — then the result.<br />Play the sequence or jump to any moment.</p><button type="button" onClick={replay}>Play conversation <span aria-hidden="true">↗</span></button></div>}
            {!!frame.entries.length && <Conversation key={generation} entries={frame.entries} appearance={appearance} agentName="Assistant" sessionId="chat-flow-demo" demo={false} canSubmit={false} submit={() => {}}
              renderReasoning={renderReasoning} renderTask={thread => thread.id in demoAgents ? <DemoAgent thread={thread} appearance={appearance} renderReasoning={renderReasoning} /> : undefined} />}
        </div>
        <footer className="motion-composer"><div>{queued && <span className="motion-queue-note">1 follow-up queued</span>}<span className="motion-local">Local replay · no API calls</span></div><p>The replay sends a follow-up at 0:14 while the edit continues.</p></footer>
      </section>
      <aside className="motion-rail" aria-label="Replay controls">
          <div className="motion-rail-intro"><span className="motion-eyebrow">CONVERSATION · REFINED</span><h2>{text.name.slice(4)}</h2><p>{text.detail}</p></div>
          <div className="motion-player"><div className="motion-play-row"><button className="motion-play" type="button" onClick={() => time >= DURATION ? replay() : setPlaying(!playing)}>{playing ? "Pause" : time >= DURATION ? "Replay" : "Play"}<Icon name={playing ? "stop" : "chevron"} size={15} /></button><button type="button" title="Start again" aria-label="Replay from the beginning" onClick={replay}>↺</button><span className="motion-time" aria-label="Replay time">{stamp(time)}<span> / {stamp(DURATION)}</span></span></div>
            <input type="range" min="0" max={DURATION} step=".05" value={time} aria-label="Conversation timeline" aria-valuetext={`${stamp(time)} of ${stamp(DURATION)}`} onChange={event => seek(event.target.valueAsNumber)} />
            <div className="motion-speed"><button type="button" onClick={() => seek(nextTime(time))}>Next event <span aria-hidden="true">→</span></button><label>Speed<select aria-label="Playback speed" value={speed} onChange={event => setSpeed(Number(event.target.value))}>{[.5, 1, 2, 4].map(value => <option key={value} value={value}>{value}×</option>)}</select></label></div>
          </div>
          <p className="motion-player-hint">Pause or scrub to inspect a partial argument. Use Appearance to choose grouped, separate, collapsed, or expanded details.</p>
          <ol className="motion-chapters" aria-label="Conversation moments">{chapters.map((chapter, index) => {
            const current = time >= chapter.at && time < (chapters[index + 1]?.at ?? Infinity);
            return <li key={chapter.at} data-current={current} data-past={time > chapter.at}><button type="button" onClick={() => seek(chapter.at)} aria-current={current ? "step" : undefined}><span className="moment-dot" /><time>{stamp(chapter.at)}</time><span>{chapter.label}</span></button></li>;
          })}</ol>
          <details className="motion-events" open={eventList} onToggle={event => setEventList(event.currentTarget.open)}><summary>Event stream <span>{frame.eventCount}</span></summary><div>{timeline.filter(item => item.at <= time).slice(-14).map((item, i) => <div key={`${item.at}-${i}`}><time>{item.at.toFixed(2)}</time><code>{item.event.event}</code></div>)}</div></details>
      </aside>
    </main>
  </div>;
}
