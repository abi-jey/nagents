import { useEffect, useRef } from "react";
import type { Entry } from "./transcript";

export function Conversation({
  entries,
  sessionId,
  busy,
  demo,
  canSubmit,
  submit,
}: {
  entries: Entry[];
  sessionId: string;
  busy: boolean;
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
            <div className="eyebrow">ngn / coding harness</div>
            <h1>
              A little less ceremony.
              <br />A little more building.
            </h1>
            <p>
              Inspect your workspace, follow the tools, and review each proposed
              action. The same ngn Harness, now in your browser.
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
                    Inspect workspace <span>&#8599;</span>
                  </button>
                  <button
                    disabled={!canSubmit}
                    onClick={() => submit("demo approval")}
                  >
                    Try an approval <span>&#8599;</span>
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
            {entry.kind === "tool" ? (
              <details>
                <summary>
                  <span className="tool-mark">&#9656;</span>
                  {entry.title || "Tool activity"}
                  <span className="detail-hint">details</span>
                </summary>
                <pre>{entry.text}</pre>
              </details>
            ) : (
              <>
                <div className="entry-label">
                  {entry.kind === "assistant"
                    ? "ngn"
                    : entry.kind === "user"
                      ? "you"
                      : entry.kind}
                </div>
                <div className="entry-content">
                  {entry.text}
                  {entry.streaming && busy && <span className="cursor" />}
                </div>
              </>
            )}
          </article>
        ))}
      </div>
    </div>
  );
}
