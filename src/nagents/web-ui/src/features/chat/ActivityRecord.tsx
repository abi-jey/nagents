import { MessageContent } from "./MessageContent.js";
import type { Entry } from "./transcript.js";

export function ActivityRecord({ entry, open, toggle }: {
  entry: Entry;
  open: boolean;
  toggle: (open: boolean) => void;
}) {
  const notification = entry.kind === "notification";
  const label = notification
    ? `${entry.cause === "wakeup" ? "Wake-up" : "Completion"} notification: ${entry.sourceName} → ${entry.taskName}`
    : "Wake-up";
  const identity = [
    ["Notification", entry.notificationId], ["Wake-up", entry.wakeupId],
    ["Run", entry.runId], ["Source task", entry.sourceTaskId],
    ["Recipient task", entry.taskId], ["Cause", entry.cause],
  ];
  return (
    <details className="activity-record" data-disclosure-key={entry.id} open={open}
      onToggle={(event) => toggle(event.currentTarget.open)}>
      <summary>
        <span className="execution-summary">
          <span className="execution-name" title={label}>{label}</span>
          <span className="execution-state" data-state={entry.state}>{notification ? "Delivered" : entry.state}</span>
        </span>
      </summary>
      <div className="execution-detail">
        {notification && <p className="record-note">Notification delivered: {entry.sourceName} to {entry.taskName}. Delivery and recipient activation are separate events.</p>}
        <dl className="request-identity">
          {identity.filter(([, value]) => value !== undefined && value !== "").map(([name, value]) => (
            <div className="identity-pair" key={name}><dt>{name}</dt><dd>{value}</dd></div>
          ))}
        </dl>
        {entry.dueAt && <p className="record-note">Due <time dateTime={entry.dueAt}>{entry.dueAt}</time></p>}
        <MessageContent text={entry.text} />
      </div>
    </details>
  );
}
