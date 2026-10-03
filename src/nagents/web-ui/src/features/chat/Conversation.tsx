import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { MessageContent } from "./MessageContent.js";
import { ActivityRecord } from "./ActivityRecord.js";
import { ChannelAttachments, ChannelHeader } from "./ChannelMessage.js";
import { LocalDeliveryCard, MediaToken } from "./LocalDelivery.js";
import { UploadAttachment, UploadSession } from "./UploadAttachment.js";
import { Icon } from "../../components/Icon.js";
import { TextLinks } from "../../components/TextLinks.js";
import { commandOutcomeDescription } from "./executionPresentation.js";
import { captionTime, conversationEntries } from "./captionPresentation.js";
import { rememberDisclosure, revealAncestors } from "../../components/disclosures.js";
import type { Bootstrap } from "../../types.js";
import {
  groupTranscript,
  type Entry,
  type TranscriptItem,
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
}: {
  items: TranscriptItem[];
  names: Map<string, string>;
  disclosures: Map<string, boolean>;
  toggle: (key: string, open: boolean) => void;
}) {
  return items.map((item) => {
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
            />
          </details>
        </section>
      );
    if (item.kind === "thread") {
      const task = item.thread;
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
              />
            </div>
          </details>
        </section>
      );
    }
    const entry = item.entry;
    return (
      <article
        key={entry.id}
        className={`entry ${entry.kind}`}
        data-record-key={entry.id}
        data-level={entry.level}
        data-speaker={entry.liveCaption?.speaker}
        data-run-id={entry.runId}
        tabIndex={-1}
      >
        {entry.liveCaption ? <>
          <div className="entry-label chat-live-caption-label">
            <span>{entry.liveCaption.speaker === "user" ? "You" : "ngn"}</span>
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
            open={disclosures.get(entry.id) ?? false}
            toggle={(open) => toggle(entry.id, open)}
            disclosures={disclosures}
            toggleDetail={toggle}
          />
        ) : entry.kind === "notification" || entry.kind === "wakeup" ? (
          <ActivityRecord entry={entry} open={disclosures.get(entry.id) ?? false}
            toggle={(open) => toggle(entry.id, open)} />
        ) : (
          <>
            <div className="entry-label">
              {entry.kind === "assistant"
                ? entry.taskId
                  ? names.get(entry.taskId) || entry.taskId
                  : "ngn"
                : entry.kind === "user"
                  ? entry.origin ? "User" : "You"
                  : entry.kind === "error"
                    ? "Error"
                    : entry.kind === "followup"
                      ? `Human follow-up ${entry.followup}`
                       : "Status"}
              {entry.origin && !entry.channel && <span className="origin-badge">{entry.origin}</span>}
              {entry.voice && <span className="origin-badge">Voice</span>}
              {entry.queued && <span className="origin-badge">{entry.admission === "queued" ? "Queued" : entry.admission === "sending" ? "Sending…" : "Delivery unconfirmed"}</span>}
            </div>
            {entry.channel && <ChannelHeader meta={entry.channel} />}
            <MessageContent text={entry.text} />
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
  entries,
  sessionId,
  demo,
  providerSetup,
  canSubmit,
  submit,
  token = "",
}: {
  entries: Entry[];
  sessionId: string;
  demo: boolean;
  providerSetup?: Bootstrap["provider_setup"];
  canSubmit: boolean;
  submit: (prompt: string) => void;
  token?: string;
}) {
  const feed = useRef<HTMLDivElement>(null);
  const contents = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const anchor = useRef<{ key: string; offset: number } | undefined>(undefined);
  const layout = useRef<{ width: number; height: number; contentHeight: number; top: number } | undefined>(undefined);
  const [newActivity, setNewActivity] = useState(false);
  const [usageTip] = useState(() => usageTips[Math.floor(Math.random() * usageTips.length)]);
  const [announcement, setAnnouncement] = useState("");
  const [disclosures, setDisclosures] = useState(new Map<string, boolean>());
  function toggle(key: string, open: boolean) {
    setDisclosures((current) => rememberDisclosure(current, key, open));
  }
  function reveal(target: HTMLElement) {
    if (!feed.current) return;
    const keys = revealAncestors(target, feed.current);
    setDisclosures((current) => keys.reduce((next, key) => rememberDisclosure(next, key, true), current));
    target.scrollIntoView({ block: "start" });
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
    if (!element) return;
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
    if (!element) return;
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
  }, [entries]);
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
  const earlier = groupTranscript(display.filter((entry) => entry.delivery?.earlier));
  const items = groupTranscript(display.filter((entry) => !entry.delivery?.earlier));
  const names = new Map(
    entries
      .filter((entry) => entry.kind === "task" && entry.taskId)
      .map((entry) => [entry.taskId!, entry.title || entry.taskId!]),
  );

  return (
    <div
      className="conversation"
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
        if (!element) return;
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
          <TranscriptItems items={earlier} names={names} disclosures={disclosures} toggle={toggle} />
        </section>}
        <TranscriptItems
          items={items}
          names={names}
          disclosures={disclosures}
          toggle={toggle}
        />
        </UploadSession.Provider>
        </MediaToken.Provider>
      </div>
    </div>
  );
}
