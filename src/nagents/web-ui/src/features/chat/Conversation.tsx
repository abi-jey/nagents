import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { DelegationActivity } from "./DelegationActivity.js";
import { DelegationFlow, type DelegationOptions } from "./delegationFlow.js";
import { TeamIcon } from "../team/TeamTree.js";
import { MessageContent } from "./MessageContent.js";
import { ActivityRecord } from "./ActivityRecord.js";
import { ResponseDetails, type ResponseDetailsProps } from "./ResponseDetails.js";
import { agentLabels, transcriptEntries } from "./agentLabels.js";
import { ActivityLayout, activityCategory, activityLabels, activityState, hasDisplayableReasoning, importantActivity } from "./activityLayout.js";
import type { ChatAppearance } from "../settings/appearance.js";
import { ChannelAttachments, ChannelHeader } from "./ChannelMessage.js";
import { LocalDeliveryCard, MediaToken } from "./LocalDelivery.js";
import { UploadAttachment, UploadSession } from "./UploadAttachment.js";
import { Icon } from "../../components/Icon.js";
import { TextLinks } from "../../components/TextLinks.js";
import { executionTarget, commandOutcomeDescription } from "./executionPresentation.js";
import { captionTime, conversationEntries } from "./captionPresentation.js";
import { rememberDisclosure, revealAncestors } from "../../components/disclosures.js";
import { scrollWithin } from "../../components/scrollWithin.js";
import type { Bootstrap } from "../../types.js";
import {
  groupTranscript,
  type Entry,
  type TranscriptItem,
  type TaskThread,
} from "./transcript.js";

const usageTips = [
  "Use Shift+Enter to add a new line to your message.",
  "Mention a file path to focus the conversation on a specific file.",
  "Start a new session when switching to an unrelated task.",
  "Return to an earlier conversation from the sessions list.",
  "Click the workspace folder to view its details.",
  "Ask for a plan before making a larger change.",
  "Expand a tool call to inspect its inputs and results.",
  "Use the sidebar divider button to make more room for your conversation.",
];

function taskAccent(id: string): number {
  let hash = 0;
  for (const character of id)
    hash = (Math.imul(hash, 31) + character.charCodeAt(0)) >>> 0;
  return hash % 6;
}

export function TranscriptItems({
  items,
  names,
  disclosures,
  toggle,
  renderTask,
  appearance,
  insideGroup = false,
  labels,
  agentName = "Assistant",
  renderReasoning,
  inspectTask,
}: {
  items: TranscriptItem[];
  appearance?: ChatAppearance;
  insideGroup?: boolean;
  names: Map<string, string>;
  disclosures: Map<string, boolean>;
  toggle: (key: string, open: boolean) => void;
  renderTask?: (thread: TaskThread) => ReactNode;
  labels?: Map<string, string>;
  agentName?: string;
  renderReasoning?: (props: ResponseDetailsProps) => ReactNode;
  inspectTask?: (id: string) => void;
}) {
  const layout = useRef(new ActivityLayout());
  const expandedRecords = useRef(new Set<string>());
  const appearanceKey = JSON.stringify(appearance);
  const expandedAppearance = useRef(appearanceKey);
  if (!items.length || expandedAppearance.current !== appearanceKey) {
    expandedAppearance.current = appearanceKey;
    expandedRecords.current.clear();
  }
  const visibleItems = renderTask ? items.filter(item => item.kind !== "retained") : items;
  const bylines = labels ?? agentLabels(transcriptEntries(items), agentName, names, appearance);
  return layout.current.render(visibleItems, appearance, insideGroup).map((item) => {
    if (item.kind === "activity_group") {
      const { label, icon } = activityLabels[item.category];
      const state = activityState(item.items);
      if (item.items.some(importantActivity)) expandedRecords.current.add(item.id);
      const delegationOnly = item.items.every(child => child.kind === "entry" && child.entry.delegation);
      const groupOpen = delegationOnly || (disclosures.get(item.id) ?? (expandedRecords.current.has(item.id) || appearance?.activity[item.category].details === "expanded"));
      const targets = [...new Set(item.items.flatMap(child => child.kind === "entry" ? [executionTarget(child.entry)] : child.kind === "thread" ? [child.thread.name] : []).filter(Boolean))].slice(0, 2).join(" · ");
      return <details key={item.id} className="chat-activity-group" data-category={item.category} data-delegation-shell={delegationOnly || undefined}
        data-state={state} data-record-key={item.id} data-disclosure-key={item.id}
        open={groupOpen}
        onToggle={event => { if (event.currentTarget.open !== groupOpen) toggle(item.id, event.currentTarget.open); }}>
        <summary hidden={delegationOnly}><Icon name={icon} size={14} /><span className="chat-activity-label">{label}<span className="chat-activity-count">{item.items.length}</span></span>
          {targets && <span className="chat-activity-target">{targets}</span>}{state && <span className="chat-activity-state" role="status">{state}</span>}</summary>
        <div className="chat-activity-body"><TranscriptItems items={item.items} names={names} disclosures={disclosures} toggle={toggle} renderTask={renderTask} appearance={appearance} labels={bylines} agentName={agentName} renderReasoning={renderReasoning} inspectTask={inspectTask} insideGroup /></div>
      </details>;
    }
    // Registry-only tasks have no proven location in this transcript. The team
    // tree still exposes them; do not attach them to its last user message.
    if (item.kind === "retained" && renderTask) return null;
    if (item.kind === "retained")
      return (
        <section
          key={item.id}
          className="retained-tasks"
          aria-label="Retained task registry"
        >
          <details
            data-disclosure-key={`registry:${item.id}`}
            open={disclosures.get(`registry:${item.id}`) ?? false}
            onToggle={(event) => toggle(`registry:${item.id}`, event.currentTarget.open)}
          >
            <summary>Retained tasks</summary>
            <p className="record-note">
              Latest registry state at reload, with subsequent live activity.
              Earlier activations and timings are not reconstructed.
            </p>
            <TranscriptItems
              items={item.items}
              names={names}
              disclosures={disclosures}
              toggle={toggle}
              renderTask={renderTask}
              appearance={appearance}
              labels={bylines}
              agentName={agentName}
              renderReasoning={renderReasoning}
            />
          </details>
        </section>
      );
    if (item.kind === "thread") {
      const task = item.thread;
      const rendered = renderTask?.(task);
      if (rendered) return <div key={`task:${task.id}`}>{rendered}</div>;
      return (
        <section
          key={`task:${task.id}`}
          className="task-thread"
          data-task-id={task.id}
          data-accent={taskAccent(task.id)}
          data-parent-task-id={task.parentId}
          data-record-key={`task:${task.id}`}
          id={`task-${task.id}`}
        >
          <details
            data-disclosure-key={`task:${task.id}`}
            open={disclosures.get(`task:${task.id}`) ?? false}
            onToggle={(event) =>
              toggle(`task:${task.id}`, event.currentTarget.open)
            }
          >
            <summary>
              <span className="execution-summary">
                <span className="execution-name" title={task.name}>
                  {task.name}
                </span>
                <span className="execution-state" data-state={task.state}>
                  {task.state}
                </span>
              </span>
            </summary>
            <div className="task-contents">
              <p className="record-identity"><strong>{task.name}</strong><br />Task {task.id}<br />
                {task.issue || `Parent: ${task.parentId ? names.get(task.parentId) || task.parentId : "Main"}`}
                {task.parentId && <><br />Parent task ID: {task.parentId}</>}
              </p>
              <TranscriptItems
                items={task.items}
                names={names}
                disclosures={disclosures}
                toggle={toggle}
                renderTask={renderTask}
                appearance={appearance}
                labels={bylines}
                agentName={agentName}
                renderReasoning={renderReasoning}
              />
            </div>
          </details>
        </section>
      );
    }
    const entry = item.entry;
    const reasoning = appearance?.activity.reasoning;
    const hasReasoning = hasDisplayableReasoning(entry, appearance);
    const keepVisible = layout.current.hasShownResponse(entry.id);
    const category = activityCategory(item);
    if (importantActivity(item)) expandedRecords.current.add(entry.id);
    const expanded = expandedRecords.current.has(entry.id) || !!category && appearance?.activity[category].details === "expanded";
    const props: ResponseDetailsProps = { metadata: entry.responseMetadata, hideSummary: reasoning?.placement === "hidden", keepVisible,
      open: disclosures.get(`reasoning:${entry.responseId}`) ?? reasoning?.details === "expanded",
      toggle: open => toggle(`reasoning:${entry.responseId}`, open) };
    const responseDetails = entry.kind === "assistant" && (hasReasoning || keepVisible && !entry.text) && (renderReasoning ? renderReasoning(props) : <ResponseDetails {...props} />);
    return (
      <article
        key={entry.id}
        className={`entry ${entry.kind}${entry.delegation ? " delegation-entry" : ""}${entry.kind === "assistant" && !entry.text ? " reasoning-only" : ""}${reasoning?.placement === "separate" ? " reasoning-separate" : ""}`}
        data-record-key={entry.id}
        data-level={entry.level}
        data-speaker={entry.liveCaption?.speaker}
        data-run-id={entry.runId}
        tabIndex={-1}
      >
        {entry.delegation ? <DelegationActivity item={entry.delegation} open={disclosures.get(entry.id) ?? expanded} toggle={open => toggle(entry.id, open)} inspect={inspectTask} /> : entry.kind === "agent_change" ? <div className="agent-change-marker" title={entry.title}><TeamIcon size={13} /><span>{entry.text}</span></div> : entry.liveCaption ? <>
          <div className="entry-label chat-live-caption-label">
            <span hidden={entry.liveCaption.speaker === "assistant" && !bylines.has(entry.id)}>{entry.liveCaption.speaker === "user" ? "You" : bylines.get(entry.id)}</span>
            <span className="chat-live-badge"><Icon name="wave" size={12} />Live</span>
            <time aria-label={`Voice time ${captionTime(entry.liveCaption.start)}`}>{captionTime(entry.liveCaption.start)}</time>
          </div>
          {entry.liveCaption.fragmentIds?.filter(id => id !== entry.id).map(id => <span key={id} data-record-key={id} className="chat-live-caption-anchor" aria-hidden="true" />)}
          <p className="chat-live-caption-text"><TextLinks text={entry.text} /></p>
        </> : entry.delivery ? <LocalDeliveryCard delivery={entry.delivery} /> : entry.kind === "tool" ||
        entry.kind === "task" ||
        entry.kind === "context" ? (
          <ExecutionRecord
            entry={entry}
            open={disclosures.get(entry.id) ?? (appearance ? expanded : entry.kind === "tool" ? undefined : false)}
            toggle={(open) => toggle(entry.id, open)}
            disclosures={disclosures}
            toggleDetail={toggle}
          />
        ) : entry.kind === "notification" || entry.kind === "wakeup" ? (
          <ActivityRecord entry={entry} open={disclosures.get(entry.id) ?? expanded}
            toggle={(open) => toggle(entry.id, open)} />
        ) : (
          <>
            <div className="entry-label" hidden={entry.kind === "assistant" && !bylines.has(entry.id)}>
              {entry.kind === "assistant"
                ? bylines.get(entry.id)
                : entry.kind === "user"
                  ? entry.origin ? "User" : "You"
                  : entry.kind === "error"
                    ? "Error"
                    : entry.kind === "followup"
                      ? `Human follow-up ${entry.followup}`
                       : "Status"}
              {entry.origin && !entry.channel && <span className="origin-badge">{entry.origin}</span>}
              {entry.voice && <span className="origin-badge">Voice</span>}
              {entry.kind === "user" && !entry.queued && ["Cancelled", "Interrupted", "Disconnected", "No result recorded"].includes(entry.state || "") &&
                <span className="origin-badge">{entry.state === "No result recorded" ? "Input not recorded" : entry.state}</span>}
              {entry.queued && <span className="origin-badge">{entry.admission === "queued" ? "Queued" : entry.admission === "sending" ? "Sending…" : "Delivery unconfirmed"}</span>}
            </div>
            {entry.channel && <ChannelHeader meta={entry.channel} />}
            {responseDetails}
            {entry.text && <MessageContent text={entry.text} />}
             {entry.uploads?.map((upload) => <UploadAttachment key={upload.upload_id} upload={upload} ready={!entry.queued} />)}
            {entry.channel && <ChannelAttachments parts={entry.parts} />}
            {entry.provenance && <details className="channel-provenance" data-disclosure-key={`source:${entry.id}`}
              open={disclosures.get(`source:${entry.id}`) ?? false} onToggle={(event) => toggle(`source:${entry.id}`, event.currentTarget.open)}>
              <summary>{entry.channelContext ? "Channel context (unverified)" : "Message provenance"}</summary>
              {entry.channelContext && <p>This context comes from message text; its channel origin is not verified.</p>}
              <pre tabIndex={0}>{entry.provenance}</pre>
            </details>}
          </>
        )}
      </article>
    );
  });
}

export function Conversation({
  entries: sourceEntries,
  sessionId,
  demo,
  providerSetup,
  canSubmit,
  submit,
  token = "",
  renderTask,
  active = true,
  appearance,
  agentName = "Assistant",
  parentAgentName = "",
  renderReasoning,
  delegation,
}: {
  entries: Entry[];
  sessionId: string;
  demo: boolean;
  providerSetup?: Bootstrap["provider_setup"];
  canSubmit: boolean;
  submit: (prompt: string) => void;
  token?: string;
  renderTask?: (thread: TaskThread) => ReactNode;
  active?: boolean;
  appearance?: ChatAppearance;
  agentName?: string;
  parentAgentName?: string;
  renderReasoning?: (props: ResponseDetailsProps) => ReactNode;
  delegation?: DelegationOptions;
}) {
  const flow = useRef(new DelegationFlow());
  const entries = delegation ? flow.current.project(sourceEntries, delegation) : sourceEntries;
  const feed = useRef<HTMLDivElement>(null);
  const isActive = useRef(active);
  isActive.current = active;
  const contents = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const anchor = useRef<{ key: string; offset: number } | undefined>(undefined);
  const layout = useRef<{ width: number; height: number; contentHeight: number; top: number } | undefined>(undefined);
  const [newActivity, setNewActivity] = useState(false);
  const [usageTip] = useState(() => usageTips[Math.floor(Math.random() * usageTips.length)]);
  const [announcement, setAnnouncement] = useState("");
  const [disclosures, setDisclosures] = useState(new Map<string, boolean>());
  const empty = entries.length === 0;
  useLayoutEffect(() => {
    if (empty) setDisclosures(current => current.size ? new Map() : current);
  }, [empty]);
  function toggle(key: string, open: boolean) {
    setDisclosures((current) => rememberDisclosure(current, key, open));
  }
  function reveal(target: HTMLElement) {
    if (!feed.current) return;
    const keys = revealAncestors(target, feed.current);
    setDisclosures((current) => keys.reduce((next, key) => rememberDisclosure(next, key, true), current));
    scrollWithin(feed.current, target, "start");
  }
  const lastActivity = useRef(0);
  const lastUser = useRef("");
  const latestActivity = entries.reduce<Entry | undefined>(
    (latest, entry) =>
      (entry.activity || 0) > (latest?.activity || 0) ? entry : latest,
    undefined,
  );
  const activity = latestActivity?.activity || 0;
  function activityTarget() {
    return [
      ...(feed.current?.querySelectorAll<HTMLElement>("[data-record-key]") ||
        []),
    ].find((item) => item.dataset.recordKey === latestActivity?.id);
  }
  function activityVisible() {
    const target = activityTarget()?.getBoundingClientRect();
    const region = feed.current?.getBoundingClientRect();
    return (
      !!target &&
      !!region &&
      target.height > 0 &&
      target.top >= region.top &&
      target.bottom <= region.bottom
    );
  }
  function inspectingTool() {
    const element = feed.current;
    const focused = element?.ownerDocument.activeElement;
    const tool = focused?.closest(".tool-record");
    if (!element || !tool || !element.contains(tool)) return false;
    const target = focused!.getBoundingClientRect();
    const region = element.getBoundingClientRect();
    return target.height > 0 && target.bottom > region.top && target.top < region.bottom;
  }
  function rememberPosition() {
    const element = feed.current;
    if (!element || !isActive.current) return;
    const region = element.getBoundingClientRect();
    const top = region.top;
    const candidates = [
      ...element.querySelectorAll<HTMLElement>("[data-record-key]"),
    ].filter((item) => {
      const rect = item.getBoundingClientRect();
      return rect.height > 0 && rect.bottom > top && rect.top < region.bottom;
    });
    // Prefer the innermost record containing the reading position, not a later
    // (possibly offscreen) record pushed down by appended output. Task ancestors
    // precede their children in DOM order, so findLast selects the actual record.
    const visible = candidates.findLast((item) => item.getBoundingClientRect().top <= top) || candidates[0];
    anchor.current = visible
      ? {
          key: visible.dataset.recordKey!,
          offset: visible.getBoundingClientRect().top - top,
        }
      : undefined;
    layout.current = { width: element.clientWidth, height: element.clientHeight, contentHeight: element.scrollHeight, top: element.scrollTop };
  }
  function restorePosition() {
    const element = feed.current;
    if (!element || !isActive.current) return;
    if (stickToBottom.current && !inspectingTool()) element.scrollTop = element.scrollHeight;
    else if (anchor.current) {
      const previous = anchor.current;
      const target = [...element.querySelectorAll<HTMLElement>("[data-record-key]")]
        .find((item) => item.dataset.recordKey === previous.key);
      if (target) element.scrollTop += target.getBoundingClientRect().top - element.getBoundingClientRect().top - previous.offset;
    }
    rememberPosition();
  }
  useLayoutEffect(() => {
    setDisclosures(current => current.size ? new Map() : current);
    stickToBottom.current = true;
    anchor.current = undefined;
    setNewActivity(false);
    setAnnouncement("");
    lastActivity.current = activity;
    if (feed.current) feed.current.scrollTop = feed.current.scrollHeight;
  }, [sessionId]);
  useLayoutEffect(() => {
    const last = entries.at(-1);
    if (last?.kind === "user" && !last.origin && last.id !== lastUser.current) {
      stickToBottom.current = true;
      lastUser.current = last.id;
    }
    restorePosition();
  }, [entries, appearance]);
  useLayoutEffect(() => { if (active) restorePosition(); }, [active]);
  useLayoutEffect(() => {
    const element = feed.current, view = element?.ownerDocument.defaultView;
    if (!element || !view) return;
    let disposed = false;
    const resize = () => { if (!disposed) restorePosition(); };
    const observer = typeof view.ResizeObserver === "function" ? new view.ResizeObserver(resize) : undefined;
    // Composer panels resize the viewport without changing entries. Observe
    // content too, for width reflow and attachments that finish loading later.
    observer?.observe(element);
    if (contents.current) observer?.observe(contents.current);
    view.addEventListener("resize", resize);
    return () => { disposed = true; observer?.disconnect(); view.removeEventListener("resize", resize); };
  }, []);
  useEffect(() => {
    if (activity <= lastActivity.current) {
      lastActivity.current = activity;
      return;
    }
    lastActivity.current = activity;
    setAnnouncement(
      latestActivity?.liveCaption
        ? `New Live caption from ${latestActivity.liveCaption.speaker === "user" ? "you" : "your assistant"}.`
        : latestActivity?.kind === "user"
        ? `New user message${latestActivity.origin ? ` from ${latestActivity.origin}` : ""}.`
        : latestActivity?.kind === "notification"
        ? `Notification delivered from ${latestActivity.sourceName} to ${latestActivity.taskName}.`
        : latestActivity?.level === "warning"
          ? latestActivity.text
          : latestActivity && commandOutcomeDescription(latestActivity)
            ? `${commandOutcomeDescription(latestActivity)}.`
          : `${latestActivity?.title || "Task activity"}: ${latestActivity?.state || "updated"}.`,
    );
    if (!activityVisible()) setNewActivity(true);
  }, [activity, latestActivity]);
  const display = conversationEntries(entries);
  const earlier = groupTranscript(display.filter((entry) => entry.delivery?.earlier), { inlineTasks: !!renderTask });
  const items = groupTranscript(display.filter((entry) => !entry.delivery?.earlier), { inlineTasks: !!renderTask });
  const names = new Map(
    entries
      .filter((entry) => entry.kind === "task" && entry.taskId)
      .map((entry) => [entry.taskId!, entry.title || entry.taskId!]),
  );
  const labels = agentLabels(display, agentName, names, appearance, parentAgentName);

  return (
    <div
      className="conversation"
      hidden={!active}
      aria-label="Conversation"
      role="region"
      tabIndex={0}
      ref={feed}
      onFocusCapture={(event) => {
        if (!event.target.closest(".tool-record")) return;
        stickToBottom.current = false;
        rememberPosition();
      }}
      onClick={(event) => {
        const href = (event.target as Element).closest("a")?.getAttribute("href");
        if (!href?.startsWith("#task-")) return;
        const target = document.getElementById(decodeURIComponent(href.slice(1)));
        if (target && feed.current?.contains(target)) {
          reveal(target);
        }
      }}
      onScroll={() => {
        const element = feed.current;
        if (!element || !isActive.current) return;
        const previous = layout.current;
        const resized = previous && (previous.width !== element.clientWidth || previous.height !== element.clientHeight || previous.contentHeight !== element.scrollHeight);
        const layoutScroll = resized && (element.scrollTop === previous.top ||
          (element.scrollTop < previous.top && element.scrollTop === Math.max(0, element.scrollHeight - element.clientHeight)));
        // A resize can emit a scroll before ResizeObserver runs. Keep the
        // existing intent and anchor for an unchanged/clamped layout offset.
        if (!layoutScroll)
          stickToBottom.current =
            element.scrollHeight - element.scrollTop - element.clientHeight <
            100 && !inspectingTool();
        if (activityVisible()) setNewActivity(false);
        if (!layoutScroll) rememberPosition();
      }}
    >
      <div className="conversation-inner" ref={contents}>
        <h2 className="sr-only">Conversation</h2>
        <div
          className="activity-announcement"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          <span className="sr-only">{announcement}</span>
          {newActivity && (
            <button
              type="button"
              onClick={() => {
                const target = activityTarget();
                if (target) reveal(target);
                setNewActivity(false);
              }}
            >
              {latestActivity?.liveCaption ? "New Live caption" : latestActivity?.kind === "user" ? "New message" : "New task activity"}
            </button>
          )}
        </div>
        {!entries.length && <p className="usage-tip"><span>Tip</span>{usageTip}</p>}
        {providerSetup && !providerSetup.configured && <section className="welcome" aria-label="Provider setup">
          <p className="demo-note" role="status">{providerSetup.message}</p>
        </section>}
        {!entries.length && demo && (
          <section className="welcome">
            <p className="demo-note">
              Offline demo. Scripted responses, real local tools and sessions.
              No provider requests, shell commands, or workspace writes.
            </p>
            <div className="starters">
              <button
                disabled={!canSubmit}
                onClick={() => submit("Show me this workspace")}
              >
                Inspect workspace
              </button>
              <button
                disabled={!canSubmit}
                onClick={() => submit("demo approval")}
              >
                Review a demo approval
              </button>
            </div>
          </section>
        )}
        <MediaToken.Provider value={token} key={sessionId}>
        <UploadSession.Provider value={sessionId}>
        {!!earlier.length && <section aria-label="Deliveries from earlier context">
          <h3>Deliveries from earlier context</h3>
          <TranscriptItems items={earlier} names={names} disclosures={disclosures} toggle={toggle} appearance={appearance} />
        </section>}
        <TranscriptItems
          items={items}
          names={names}
          disclosures={disclosures}
          toggle={toggle}
          renderTask={renderTask}
          inspectTask={delegation?.inspect}
          appearance={appearance}
          labels={labels}
          agentName={agentName}
          renderReasoning={renderReasoning}
        />
        </UploadSession.Provider>
        </MediaToken.Provider>
      </div>
    </div>
  );
}
