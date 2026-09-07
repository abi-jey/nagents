import { useEffect, useRef } from "react";
import { ExecutionRecord } from "./ExecutionRecord";
import { MessageContent } from "./MessageContent";
import type { Entry } from "./transcript";

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
  useEffect(() => {
    stickToBottom.current = true;
    if (feed.current) feed.current.scrollTop = feed.current.scrollHeight;
  }, [sessionId]);
  useEffect(() => {
    if (entries.at(-1)?.kind === "user") stickToBottom.current = true;
    if (stickToBottom.current && feed.current)
      feed.current.scrollTop = feed.current.scrollHeight;
  }, [entries]);

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
      }}
    >
      <div className="conversation-inner">
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
        {entries.map((entry, index) => (
          <article key={index} className={`entry ${entry.kind}`}>
            {entry.kind === "tool" || entry.kind === "task" ? (
              <ExecutionRecord entry={entry} />
            ) : (
              <>
                <div className="entry-label">
                  {entry.kind === "assistant"
                    ? "ngn"
                    : entry.kind === "user"
                      ? "You"
                      : entry.kind === "error"
                        ? "Error"
                        : "Status"}
                </div>
                <MessageContent text={entry.text} />
              </>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}
