import type { MessagePart } from "../../types.js";
import type { ChannelMeta } from "./channelMessage.js";

// Channel messages render as their own card: a trusted header line built from the
// verified backend source annotation, the message text, and attachment previews.
export function ChannelHeader({ meta }: { meta: ChannelMeta }) {
  const who =
    meta.senderName && meta.senderUsername
      ? `${meta.senderName} (@${meta.senderUsername})`
      : meta.senderName || (meta.senderUsername ? `@${meta.senderUsername}` : "");
  return (
    <div className="channel-header" data-channel={meta.channel}>
      {meta.sentAt && (
        <time className="channel-time" dateTime={meta.sentAt}>
          {meta.sentAt}
        </time>
      )}
      {meta.channel && <span className="channel-chip">{meta.channel}</span>}
      {meta.conversationType && <span className="channel-chip">{meta.conversationType}</span>}
      {who && <span className="channel-sender">{who}</span>}
      {meta.senderId && <span className="channel-id">user {meta.senderId}</span>}
      {meta.conversationId && <span className="channel-id">chat {meta.conversationId}</span>}
      {meta.replyTo && <span className="channel-id">reply {meta.replyTo}</span>}
      <span className="channel-kind">{meta.kind}</span>
    </div>
  );
}

export function ChannelAttachments({ parts }: { parts?: MessagePart[] }) {
  const files = (parts ?? []).filter((part) => part.type !== "text");
  if (!files.length) return null;
  return (
    <div className="channel-attachments">
      {files.map((part, index) =>
        part.type === "image" ? (
          <img
            key={index}
            className="channel-image"
            src={`data:${part.media_type};base64,${part.data_base64}`}
            alt="Channel image attachment"
          />
        ) : part.type === "document" ? (
          <span className="channel-file" key={index}>
            {part.title || part.media_type}
          </span>
        ) : (
          <span className="channel-file" key={index}>
            audio · {part.format}
          </span>
        ),
      )}
    </div>
  );
}
