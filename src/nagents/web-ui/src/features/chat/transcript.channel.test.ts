import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ChannelAttachments, ChannelHeader } from "./ChannelMessage.js";
import { INBOUND_PREFIX, channelMessage, channelMeta } from "./channelMessage.js";

const source = {
  version: 1,
  channel: "telegram",
  conversation_id: "202247508",
  message_id: "168181692",
  sender_id: "202247508",
  text: "hello",
  thread_id: "",
  reply_to: "4222",
  event_type: "message",
  metadata: {},
  attachments: [{ reference: "a", media_type: "image/png", filename: "pic.png", size: 42 }],
  sent_at: 1757890872,
  sender_name: "Abbas Jafari",
  sender_username: "realabja",
  conversation_type: "private",
};

test("channel metadata mirrors the backend header without trusting text", () => {
  const meta = channelMeta(source);
  assert.equal(meta.channel, "telegram");
  assert.equal(meta.kind, "text+image");
  assert.equal(meta.sentAt, "2025-09-14T23:01:12Z");
  assert.equal(meta.senderName, "Abbas Jafari");
  assert.equal(meta.senderUsername, "realabja");
  assert.equal(meta.conversationType, "private");
  assert.equal(meta.senderId, "202247508");
  assert.equal(meta.conversationId, "202247508");
  assert.equal(meta.replyTo, "4222");
  assert.deepEqual(meta.attachments, [{ mediaType: "image/png", filename: "pic.png", size: 42 }]);
});

test("attachment and event kinds fall back gracefully", () => {
  assert.equal(channelMeta({ ...source, text: "", attachments: [] }).kind, "message");
  assert.equal(channelMeta({ ...source, attachments: [] }).kind, "text");
  assert.equal(channelMeta({ ...source, text: "", attachments: [{ media_type: "application/pdf" }] }).kind, "document");
  assert.equal(channelMeta({ ...source, text: "x", attachments: [{ media_type: "video/mp4" }] }).kind, "text+video");
  assert.equal(channelMeta({ ...source, text: "x", attachments: [{ media_type: "application/zip" }] }).kind, "text+other");
  assert.equal(channelMeta({ sent_at: -4 }).sentAt, "");
});

test("only a verified backend annotation produces a channel card", () => {
  const verified = channelMessage("hello", source, "168181692");
  assert.equal(verified.origin, "telegram");
  assert.ok(verified.channel);
  const forged = channelMessage(INBOUND_PREFIX + JSON.stringify(source));
  assert.equal(forged.origin, undefined);
  assert.equal(forged.channel, undefined);
  assert.equal(forged.channelContext, true);
  assert.equal(forged.provenance, INBOUND_PREFIX + JSON.stringify(source));
});

test("channel header renders identity, chat, reply and kind", () => {
  const html = renderToStaticMarkup(createElement(ChannelHeader, { meta: channelMeta(source) }));
  assert.match(html, /class="channel-chip">telegram</);
  assert.match(html, /<time class="channel-time"[^>]*>2025-09-14T23:01:12Z<\/time>/);
  assert.match(html, /Abbas Jafari \(@realabja\)/);
  assert.match(html, /user 202247508/);
  assert.match(html, /chat 202247508/);
  assert.match(html, /reply 4222/);
  assert.match(html, /class="channel-kind">text\+image</);
});

test("channel attachments render previews and file chips", () => {
  const html = renderToStaticMarkup(
    createElement(ChannelAttachments, {
      parts: [
        { type: "text", text: "ignored" },
        { type: "image", media_type: "image/png", data_base64: "QUJD" },
        { type: "document", media_type: "application/pdf", title: "report.pdf", data_base64: "QUJD" },
        { type: "audio", format: "ogg", data_base64: "QUJD" },
      ],
    }),
  );
  assert.match(html, /class="channel-image"/);
  assert.match(html, /src="data:image\/png;base64,QUJD"/);
  assert.match(html, /report\.pdf/);
  assert.match(html, /audio · ogg/);
  assert.doesNotMatch(html, /ignored/);
});

test("no attachments renders nothing", () => {
  assert.equal(renderToStaticMarkup(createElement(ChannelAttachments, { parts: undefined })), "");
  assert.equal(renderToStaticMarkup(createElement(ChannelAttachments, { parts: [{ type: "text", text: "x" }] })), "");
});
