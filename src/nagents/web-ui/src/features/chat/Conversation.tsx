import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { MessageContent } from "./MessageContent.js";
import {
  groupTranscript,
  type Entry,
  type TranscriptItem,
} from "./transcript.js";

function taskAccent(id: string): number {
  let hash = 0;
  for (const character of id)
    hash = (Math.imul(hash, 31) + character.charCodeAt(0)) >>> 0;
  return hash % 6;
}

function TranscriptItems({
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
          <h2 className="entry-label">Retained tasks</h2>
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
            open={disclosures.get(`task:${task.id}`) ?? true}
            onToggle={(event) =>
              toggle(`task:${task.id}`, event.currentTarget.open)
            }
          >
            <summary>
              <span className="execution-summary">
                <span className="execution-name">
                  {task.name}
                  <small className="record-identity">Task {task.id}</small>
                  <small className="record-identity">
                    {task.issue ||
                      `Parent: ${task.parentId ? names.get(task.parentId) || task.parentId : "Main"}`}
                  </small>
                </span>
                <span className="execution-state" data-state={task.state}>
                  {task.state}
                </span>
              </span>
            </summary>
            <div className="task-contents">
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
      >
        {entry.kind === "tool" ||
        entry.kind === "task" ||
        entry.kind === "context" ? (
          <ExecutionRecord
            entry={entry}
            open={disclosures.get(entry.id) ?? false}
            toggle={(open) => toggle(entry.id, open)}
          />
        ) : entry.kind === "notification" ? (
          <>
            <div className="entry-label">
              Notification delivered: {entry.sourceName} to {entry.taskName}
              <small
                className="record-identity"
                title="Delivery and recipient activation are separate events."
              >
                {entry.cause === "wakeup" ? "Wake-up" : "Completion"}{" "}
                notification
              </small>
            </div>
            <MessageContent text={entry.text} />
          </>
        ) : entry.kind === "wakeup" ? (
          <>
            <div className="execution-summary">
              <span className="execution-name">
                Wake-up
                <small className="record-identity">{entry.wakeupId}</small>
              </span>
              <span className="execution-state" data-state={entry.state}>
                {entry.state}
              </span>
            </div>
            {entry.dueAt && (
              <p className="record-note">
                Due <time dateTime={entry.dueAt}>{entry.dueAt}</time>
              </p>
            )}
            <MessageContent text={entry.text} />
          </>
        ) : (
          <>
            <div className="entry-label">
              {entry.kind === "assistant"
                ? entry.taskId
                  ? names.get(entry.taskId) || entry.taskId
                  : "ngn"
                : entry.kind === "user"
                  ? "You"
                  : entry.kind === "error"
                    ? "Error"
                    : entry.kind === "followup"
                      ? `Human follow-up ${entry.followup}`
                      : "Status"}
            </div>
            <MessageContent text={entry.text} />
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
  canSubmit,
  submit,
}: {
  entries: Entry[];
  sessionId: string;
  demo: boolean;
  canSubmit: boolean;
  submit: (prompt: string) => void;
}) {
  const feed = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);
  const anchor = useRef<{ key: string; offset: number } | undefined>(undefined);
  const [newActivity, setNewActivity] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  const [disclosures, setDisclosures] = useState(new Map<string, boolean>());
  function toggle(key: string, open: boolean) {
    setDisclosures((current) => {
      if ((current.get(key) ?? key.startsWith("task:")) === open)
        return current;
      return new Map(current).set(key, open);
    });
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
  function rememberPosition() {
    const element = feed.current;
    if (!element) return;
    const top = element.getBoundingClientRect().top;
    const visible = [
      ...element.querySelectorAll<HTMLElement>("[data-record-key]"),
    ].find(
      (item) =>
        item.getBoundingClientRect().bottom > top &&
        item.getBoundingClientRect().top >= top - 1,
    );
    anchor.current = visible
      ? {
          key: visible.dataset.recordKey!,
          offset: visible.getBoundingClientRect().top - top,
        }
      : undefined;
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
    if (last?.kind === "user" && last.id !== lastUser.current) {
      stickToBottom.current = true;
      lastUser.current = last.id;
    }
    if (stickToBottom.current && feed.current)
      feed.current.scrollTop = feed.current.scrollHeight;
    else if (feed.current && anchor.current) {
      const previous = anchor.current;
      const target = [
        ...feed.current.querySelectorAll<HTMLElement>("[data-record-key]"),
      ].find((item) => item.dataset.recordKey === previous.key);
      if (target)
        feed.current.scrollTop +=
          target.getBoundingClientRect().top -
          feed.current.getBoundingClientRect().top -
          previous.offset;
    }
    rememberPosition();
  }, [entries]);
  useEffect(() => {
    if (activity <= lastActivity.current) {
      lastActivity.current = activity;
      return;
    }
    lastActivity.current = activity;
    setAnnouncement(
      latestActivity?.kind === "notification"
        ? `Notification delivered from ${latestActivity.sourceName} to ${latestActivity.taskName}.`
        : latestActivity?.level === "warning"
          ? latestActivity.text
          : `${latestActivity?.title || "Task activity"}: ${latestActivity?.state || "updated"}.`,
    );
    if (!activityVisible()) setNewActivity(true);
  }, [activity, latestActivity]);
  const items = groupTranscript(entries);
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
      onScroll={() => {
        const element = feed.current;
        if (element)
          stickToBottom.current =
            element.scrollHeight - element.scrollTop - element.clientHeight <
            100;
        if (activityVisible()) setNewActivity(false);
        rememberPosition();
      }}
    >
      <div className="conversation-inner">
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
                let ancestor = target?.parentElement;
                while (ancestor && ancestor !== feed.current) {
                  if (ancestor instanceof HTMLDetailsElement)
                    ancestor.open = true;
                  ancestor = ancestor.parentElement;
                }
                target?.scrollIntoView({ block: "start" });
                setNewActivity(false);
              }}
            >
              New task activity
            </button>
          )}
        </div>
        {!entries.length && (
          <section className="welcome">
            <h2>Start with the workspace</h2>
            <p>
              Ask ngn to inspect a file, explain a problem, or propose a change.
              Tool calls stay in the transcript so you can review their inputs
              and results.
            </p>
            {demo ? (
              <>
                <p className="demo-note">
                  Offline demo. Scripted responses, real local tools and
                  sessions. No provider requests, shell commands, or workspace
                  writes.
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
              </>
            ) : (
              <p className="demo-note">
                Live provider requests may incur costs. Shell and edits require
                approval. This is a trusted local tool, not a sandbox.
              </p>
            )}
          </section>
        )}
        <TranscriptItems
          items={items}
          names={names}
          disclosures={disclosures}
          toggle={toggle}
        />
      </div>
    </div>
  );
}
