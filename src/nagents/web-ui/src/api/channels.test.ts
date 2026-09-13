import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { buildSave, installCommand, makeDraft, rootSessions } from "../features/channels/draft.js";
import { channelRequest } from "../features/channels/transport.js";
import { ChannelsDialog } from "../features/channels/ChannelsDialog.js";
import type { Catalog, ChannelDraft, ChannelSave } from "../features/channels/types.js";
import type { useChannels } from "../features/channels/useChannels.js";
import { rememberDisclosure, revealInvalidField } from "../components/disclosures.js";

const sessions = [
  { id: "main-root", title: "Main", updated_at: "today" },
  { id: "telegram-chat", title: "Telegram chat", updated_at: "today" },
  { id: "child", title: "Nested agent", updated_at: "today", parent_session_id: "main-root" },
];
const catalog: Catalog = {
  revision: "revision-1", plugin_path: "/data/channel plugins", bindings: [],
  plugins: [{ id: "telegram", name: "Telegram", description: "Telegram connector", version: "0.6a1", schema: {
    type: "object", required: ["token", "label"], properties: {
      token: { type: "string", writeOnly: true, default: "never-show-secret-default" },
      label: { type: "string", minLength: 2 }, count: { type: "integer", minimum: 1, maximum: 10 },
      enabled_feature: { type: "boolean" }, chats: { type: "array", items: { type: "number" } },
      mode: { type: "string", enum: ["personal", "team"] }, options: { type: "object" },
    },
  } }],
  connections: [{ id: "personal", plugin: "telegram", enabled: true, status: "running", error: "",
    config: { label: "Personal", count: 2, token: "server-must-not-return-this" }, secret_fields: ["token"], configured_secrets: ["token"], main_session_id: "main-root" }],
};

// Offline fixture of nagents_channel_telegram_bot._plugin.plugin's descriptor.
// Keep connector-specific rules here rather than hard-coding a Telegram form.
const telegram: Catalog = {
  ...catalog, connections: [], plugins: [{ id: "telegram", name: "Telegram Bot", version: "0.6a1",
    description: "Telegram long polling, explicit text/edit/delete tools, host session commands and typing indicators.",
    schema: { type: "object", properties: {
      name: { type: "string", description: "Stable channel name; the web host injects its connection ID.", pattern: "^[A-Za-z_][A-Za-z0-9_.-]{0,63}$", readOnly: true },
      token: { type: "string", description: "Bot token, privately injected by the host. Omit when using token_env.", writeOnly: true, minLength: 1 },
      token_env: { type: "string", description: "Environment variable containing the token. Omit when providing token directly.", pattern: "^[A-Za-z_][A-Za-z0-9_]*$" },
      allowed_chat_ids: { type: "array", description: "Admit these numeric chat IDs; empty admits all visible supported chats.", items: { type: "string", pattern: "^-?[1-9][0-9]{0,18}$" }, default: [] },
      poll_timeout: { type: "integer", description: "Long-poll timeout in seconds.", minimum: 1, maximum: 50, default: 30 },
    }, additionalProperties: false },
  }],
};

test("schema drafts separate write-only secrets and save empty inputs as keep unless explicitly cleared", () => {
  const draft = makeDraft(catalog, "telegram-chat", catalog.connections[0]);
  assert.equal(draft.mainSessionId, "main-root"); assert.equal(draft.id, "personal");
  assert.deepEqual(draft.secrets, {}); assert.equal(draft.fields.token, undefined); assert.doesNotMatch(draft.json, /token/);
  let result = buildSave(catalog, draft, sessions); assert.deepEqual(result.payload?.secrets, {});
  result = buildSave(catalog, { ...draft, secrets: { token: "new-private-token" } }, sessions);
  assert.deepEqual(result.payload?.secrets, { token: "new-private-token" }); assert.equal(result.payload?.config.token, undefined);
  result = buildSave(catalog, { ...draft, clear: ["token"] }, sessions);
  assert.match(result.errors.token, /required/);
  const optional = { ...catalog, plugins: [{ ...catalog.plugins[0], schema: { ...catalog.plugins[0].schema, required: ["label"] } }] };
  result = buildSave(optional, { ...draft, clear: ["token"], secrets: { token: "ignored" } }, sessions);
  assert.deepEqual(result.payload?.secrets, { token: "" });
});
test("new connections select the current root but cannot persist child, missing, or mutable routing identities", () => {
  const draft = { ...makeDraft(catalog, "telegram-chat"), id: "telegram-new", fields: { label: "New" }, secrets: { token: "private" } };
  assert.equal(buildSave(catalog, draft, sessions).payload?.main_session_id, "telegram-chat");
  for (const id of ["", "child", "deleted"]) assert.ok(buildSave(catalog, { ...draft, mainSessionId: id }, sessions).errors.mainSessionId);
  for (const id of ["../route", "bad/route", "white space", "-option"]) assert.ok(buildSave(catalog, { ...draft, id }, sessions).errors.id);
  assert.deepEqual(rootSessions(sessions).map((session) => session.id), ["main-root", "telegram-chat"]);
});
test("generated number/bool/array/enum fields and generic nested JSON are typed and validated before save", () => {
  const draft = makeDraft(catalog, "main-root", catalog.connections[0]);
  const fields = { ...draft.fields, count: "3", enabled_feature: "false", chats: "[123,456]", mode: '"team"' };
  const valid = buildSave(catalog, { ...draft, fields, json: '{"options":{"nested":true}}' }, sessions);
  assert.deepEqual(valid.payload?.config, { options: { nested: true }, label: "Personal", count: 3, enabled_feature: false, chats: [123, 456], mode: "team" });
  for (const [key, value] of [["count", "1.5"], ["count", "11"], ["enabled_feature", "0"], ["chats", '["not-a-number"]'], ["mode", '"unknown"'], ["label", "x"]])
    assert.ok(buildSave(catalog, { ...draft, fields: { ...fields, [key]: value } }, sessions).errors[key]);
  for (const json of ["[]", "null", "bad", '{"token":"private"}', '{"count":4}'])
    assert.ok(buildSave(catalog, { ...draft, json }, sessions).errors.json);
});
test("plain factory schema falls back to a public JSON object", () => {
  const plain = { ...catalog, plugins: [{ ...catalog.plugins[0], schema: {} }], connections: [] };
  const draft = { ...makeDraft(plain, "main-root"), id: "factory", json: '{"nested":{"values":[true,3,"text"]}}' };
  assert.deepEqual(buildSave(plain, draft, sessions).payload?.config, { nested: { values: [true, 3, "text"] } });
});
test("generic factory secrets and nested write-only properties stay out of public configuration", () => {
  const plain = { ...catalog, plugins: [{ ...catalog.plugins[0], schema: {} }], connections: [] };
  const draft = { ...makeDraft(plain, "main-root"), id: "factory", secrets: { api_token: "private" } };
  assert.deepEqual(buildSave(plain, draft, sessions).payload?.secrets, { api_token: "private" });
  assert.ok(buildSave(plain, { ...draft, json: '{"api_token":"private"}' }, sessions).errors.json);
  const nested = { ...plain, plugins: [{ ...plain.plugins[0], schema: { properties: {
    credentials: { type: "object", properties: { token: { type: "string", writeOnly: true } } },
  } } }] };
  const privateDraft = { ...makeDraft(nested, "main-root"), id: "factory", secrets: { credentials: '{"token":"private"}' } };
  const payload = buildSave(nested, privateDraft, sessions).payload;
  assert.deepEqual(payload?.config, {}); assert.deepEqual(payload?.secrets, { credentials: { token: "private" } });
});
test("instance name from plugin schemas is supplied only through the stable routing ID", () => {
  const named = { ...catalog, plugins: [{ ...catalog.plugins[0], schema: { required: ["name"], properties: { name: { type: "string", default: "wrong-name" } } } }] };
  const draft = { ...makeDraft(named, "main-root"), id: "routing-name" };
  assert.equal(draft.fields.name, undefined); assert.equal(buildSave(named, draft, sessions).payload?.config.name, undefined);
});
test("install command safely quotes plugin path and accepts distribution names rather than shell or pip options", () => {
  assert.equal(installCommand("/data/channel plugins", "nagents-channel-telegram-bot==0.6.0a1"),
    "python -m pip install --target '/data/channel plugins' --no-deps 'nagents-channel-telegram-bot==0.6.0a1'");
  assert.match(installCommand("/data/it's/plugins", "package"), /'"'"'/);
  for (const name of [";touch bad", "--user", "package && whoami", "https://example.invalid/plugin", "-r requirements", "$(whoami)", "package\nother"])
    assert.equal(installCommand("/data/plugins", name), "");
  assert.equal(installCommand("/data\nplugins", "package"), "");
});
test("channel transport discovers installed plugins, explicitly refreshes and sends revision-bound PUT/DELETE", async (context) => {
  const calls: { url: string; options: RequestInit }[] = [];
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    if (options.method !== "GET") {
      assert.equal(new Headers(options.headers).get("Content-Type"), "application/json");
      assert.doesNotThrow(() => JSON.parse(options.body as string));
    }
    calls.push({ url, options }); return Response.json(catalog);
  });
  const signal = new AbortController().signal;
  const loaded = await channelRequest("token", signal, "load"); assert.equal(loaded.plugins[0].id, "telegram");
  await channelRequest("token", signal, "refresh");
  const payload = buildSave(catalog, makeDraft(catalog, "main-root", catalog.connections[0]), sessions).payload!;
  await channelRequest("token", signal, "save", "personal", payload);
  await channelRequest("token", signal, "delete", "personal", { revision: catalog.revision });
  assert.deepEqual(calls.map(({ url, options }) => [url, options.method]), [
    ["/api/channels", "GET"], ["/api/channels/refresh", "POST"], ["/api/channels/personal", "PUT"], ["/api/channels/personal", "DELETE"],
  ]);
  assert.deepEqual(JSON.parse(calls[1].options.body as string), {});
  assert.equal(JSON.parse(calls[2].options.body as string).revision, "revision-1");
  assert.deepEqual(JSON.parse(calls[3].options.body as string), { revision: "revision-1" });
});
test("conflict leaves the draft intact and errors never echo raw server credentials", async (context) => {
  const draft = makeDraft(catalog, "main-root", catalog.connections[0]);
  draft.secrets.token = "private-draft"; const before = structuredClone(draft);
  context.mock.method(globalThis, "fetch", async () => Response.json({ detail: "https://token:credential@private.invalid" }, { status: 409 }));
  await assert.rejects(channelRequest("token", new AbortController().signal, "save", draft.id, buildSave(catalog, draft, sessions).payload),
    (error: Error) => /draft is kept/.test(error.message) && !/credential|private.invalid/.test(error.message));
  assert.deepEqual(draft, before);
});
test("a cancelled catalog read cannot return late data from an old connection", async (context) => {
  const controller = new AbortController();
  context.mock.method(globalThis, "fetch", async () => {
    controller.abort(); return Response.json(catalog);
  });
  await assert.rejects(channelRequest("old-token", controller.signal, "load"), { name: "AbortError" });
});
test("compact accessible panel exposes main picker, password-only secrets, status and explicit save without secret GET values", () => {
  const draft = makeDraft(catalog, "main-root", catalog.connections[0]);
  const channels: ReturnType<typeof useChannels> = {
    open: true, catalog, draft, editing: "personal", pending: false, error: "", notice: "", errors: {}, conflict: false,
    choose: () => undefined, update: () => undefined, refresh: async () => undefined, show: () => undefined,
    save: async () => undefined, remove: async () => undefined, close: () => undefined,
  };
  const html = renderToStaticMarkup(createElement(ChannelsDialog, { channels, sessions, selected: "main-root" }));
  assert.match(html, /<dialog[^>]*aria-labelledby="channels-title"/);
  assert.match(html, /id="channel-token" type="password"/); assert.match(html, /Configured/);
  assert.match(html, /Clear saved token on Save/); assert.match(html, /id="channel-id"[^>]*readOnly=""/);
  assert.match(html, /Main session/); assert.match(html, /Telegram chat/); assert.doesNotMatch(html, /Nested agent/);
  assert.match(html, /Refresh installed plugins/); assert.match(html, /Copy install command/); assert.match(html, />Save<\/button>/);
  assert.doesNotMatch(html, /never-show-secret-default|server-must-not-return-this/);
});
test("exact Telegram descriptor defaults omit both credentials unless a token or environment reference is supplied", () => {
  const draft = { ...makeDraft(telegram, "main-root"), id: "telegram-personal" };
  assert.deepEqual(draft.fields, { token_env: "", allowed_chat_ids: "[]", poll_timeout: "30" });
  assert.deepEqual(draft.secrets, {}); assert.equal(draft.json, "{}");
  const defaults = buildSave(telegram, draft, sessions).payload!;
  assert.deepEqual(defaults.config, { allowed_chat_ids: [], poll_timeout: 30 });
  assert.deepEqual(defaults.secrets, {}, "Neither credential key lets the factory choose its default environment");
  const direct = buildSave(telegram, { ...draft, secrets: { token: "mock-token" } }, sessions).payload!;
  assert.deepEqual(direct.secrets, { token: "mock-token" });
  assert.equal(Object.hasOwn(direct.config, "token_env"), false);
  assert.equal(Object.hasOwn(direct.config, "name"), false);
});
test("Telegram credential switch sends a complete PUT replacement without the old public token_env key", async (context) => {
  const previous: Catalog = { ...telegram, connections: [{
    id: "telegram-personal", plugin: "telegram", enabled: true, status: "running", error: "",
    config: { token_env: "OLD_TELEGRAM_TOKEN", allowed_chat_ids: ["-100123"], poll_timeout: 20 },
    secret_fields: ["token"], configured_secrets: [], main_session_id: "main-root",
  }] };
  const draft = makeDraft(previous, "main-root", previous.connections[0]);
  assert.equal(draft.fields.token_env, "OLD_TELEGRAM_TOKEN");
  const result = buildSave(previous, { ...draft, fields: { ...draft.fields, token_env: "" }, secrets: { token: "mock-private-token" } }, sessions);
  assert.deepEqual(result.errors, {}); assert.ok(result.payload);
  context.mock.method(globalThis, "fetch", async (url: string, options: RequestInit) => {
    assert.equal(url, "/api/channels/telegram-personal"); assert.equal(options.method, "PUT");
    const body = JSON.parse(options.body as string) as ChannelSave;
    assert.deepEqual(body.config, { allowed_chat_ids: ["-100123"], poll_timeout: 20 });
    assert.equal(Object.hasOwn(body.config, "token_env"), false, "An empty string would still conflict with token in the factory");
    assert.equal(Object.hasOwn(body.config, "name"), false);
    assert.deepEqual(body.secrets, { token: "mock-private-token" });
    assert.equal(body.revision, previous.revision);
    return Response.json({ ...previous, revision: "saved-revision", connections: [{
      ...previous.connections[0], config: body.config, configured_secrets: ["token"],
    }] });
  });
  const saved = await channelRequest("process-token", new AbortController().signal, "save", draft.id, result.payload);
  const roundTrip = makeDraft(saved, "main-root", saved.connections[0]);
  assert.equal(roundTrip.fields.token_env, ""); assert.deepEqual(roundTrip.secrets, {});
  const nextSave = buildSave(saved, roundTrip, sessions).payload!;
  assert.equal(Object.hasOwn(nextSave.config, "token_env"), false);
  assert.deepEqual(nextSave.secrets, {}, "Blank password input preserves the saved token");
  assert.equal(previous.connections[0].config.token_env, "OLD_TELEGRAM_TOKEN", "Building the replacement does not mutate the saved catalog");
});
test("Telegram secret omission keeps the saved token, while explicit clear permits switching to token_env", () => {
  const saved: Catalog = { ...telegram, connections: [{
    id: "telegram-personal", plugin: "telegram", enabled: true, status: "running", error: "",
    config: { allowed_chat_ids: [], poll_timeout: 30 }, secret_fields: ["token"], configured_secrets: ["token"], main_session_id: "main-root",
  }] };
  const draft = makeDraft(saved, "main-root", saved.connections[0]);
  assert.deepEqual(buildSave(saved, { ...draft, secrets: { token: "" } }, sessions).payload?.secrets, {});
  const switched = buildSave(saved, { ...draft, fields: { ...draft.fields, token_env: "NEW_TELEGRAM_TOKEN" }, clear: ["token"] }, sessions);
  assert.deepEqual(switched.errors, {});
  assert.deepEqual(switched.payload?.config, { token_env: "NEW_TELEGRAM_TOKEN", allowed_chat_ids: [], poll_timeout: 30 });
  assert.deepEqual(switched.payload?.secrets, { token: "" }, "The host removes this key before constructing the connector");
});
test("Telegram patterns, string chat IDs and integer timeout limits validate against its descriptor", () => {
  const draft = { ...makeDraft(telegram, "main-root"), id: "telegram-personal" };
  for (const [key, value] of [["token_env", " "], ["token_env", "9TOKEN"], ["token_env", "TOKEN-NAME"],
    ["allowed_chat_ids", "[123]"], ["allowed_chat_ids", '["0"]'], ["allowed_chat_ids", '["001"]'], ["allowed_chat_ids", '["text"]'],
    ["poll_timeout", "0"], ["poll_timeout", "51"], ["poll_timeout", "1.5"]]) {
    const result = buildSave(telegram, { ...draft, fields: { ...draft.fields, [key]: value } }, sessions);
    assert.ok(result.errors[key], `${key} must reject ${value}`); assert.equal(result.payload, undefined);
  }
  for (const timeout of ["1", "50"]) {
    const result = buildSave(telegram, { ...draft, fields: { token_env: "VALID_TOKEN_2", allowed_chat_ids: '["123","-100123"]', poll_timeout: timeout } }, sessions);
    assert.deepEqual(result.errors, {}); assert.equal(result.payload?.config.poll_timeout, Number(timeout));
  }
  assert.ok(buildSave(telegram, { ...draft, json: '{"unknown":true}' }, sessions).errors.json);
});
test("read-only host fields stay outside generated fields and complete public config, including legacy saved name", () => {
  const managed: Catalog = { ...telegram, plugins: [{ ...telegram.plugins[0], schema: {
    ...telegram.plugins[0].schema, required: ["name", "host_revision"],
    properties: { ...telegram.plugins[0].schema.properties, host_revision: { type: "string", readOnly: true } },
  } }], connections: [{ ...catalog.connections[0], plugin: "telegram", config: { name: "host-injected-name", host_revision: "host-value" }, configured_secrets: [] }] };
  const draft = makeDraft(managed, "main-root", managed.connections[0]);
  assert.equal(draft.fields.name, undefined); assert.equal(draft.fields.host_revision, undefined);
  assert.equal(draft.json, "{}");
  assert.deepEqual(buildSave(managed, draft, sessions).payload?.config, { allowed_chat_ids: [], poll_timeout: 30 });
  assert.ok(buildSave(managed, { ...draft, json: '{"name":"user-supplied"}' }, sessions).errors.json);
  assert.ok(buildSave(managed, { ...draft, json: '{"host_revision":"user-supplied"}' }, sessions).errors.json);
  assert.ok(buildSave(managed, { ...draft, secrets: { host_revision: "user-supplied" } }, sessions).errors.json);
});
test("Telegram form exposes credential switching help and never offers an editable name property", () => {
  const draft = { ...makeDraft(telegram, "main-root"), id: "telegram-personal" };
  const channels: ReturnType<typeof useChannels> = {
    open: true, catalog: telegram, draft, editing: "", pending: false, error: "", notice: "", errors: {}, conflict: false,
    choose: () => undefined, update: () => undefined, refresh: async () => undefined, show: () => undefined,
    save: async () => undefined, remove: async () => undefined, close: () => undefined,
  };
  const html = renderToStaticMarkup(createElement(ChannelsDialog, { channels, sessions, selected: "main-root" }));
  assert.match(html, /id="channel-token" type="password"/);
  assert.match(html, /id="channel-token_env"/);
  assert.match(html, /Leave empty to omit this property; Save removes its previous public value/);
  assert.match(html, /Omit when using token_env/); assert.match(html, /Omit when providing token directly/);
  assert.match(html, /Stable instance ID/); assert.doesNotMatch(html, /id="channel-name"/);
});

function renderPanel(catalog: Catalog, draft = makeDraft(catalog, "main-root", catalog.connections[0]), errors: Record<string, string> = {}) {
  const channels: ReturnType<typeof useChannels> = {
    open: true, catalog, draft, editing: catalog.connections[0]?.id || "", pending: false, error: "", notice: "", errors, conflict: false,
    choose: () => undefined, update: () => undefined, refresh: async () => undefined, show: () => undefined,
    save: async () => undefined, remove: async () => undefined, close: () => undefined,
  };
  return renderToStaticMarkup(createElement(ChannelsDialog, { channels, sessions, selected: "main-root" }));
}

// Inspect native disclosure ancestry, not a whole-markup snapshot: a control can
// exist in the DOM while still being hidden/inaccessible in a collapsed section.
function fieldLocation(html: string, id: string) {
  const ancestors: string[] = [];
  for (const match of html.matchAll(/<\/?details\b[^>]*>|<(?:input|select|textarea)\b[^>]*>/g)) {
    const tag = match[0];
    if (tag.startsWith("</details")) ancestors.pop();
    else if (tag.startsWith("<details")) ancestors.push(tag);
    else if (tag.includes(`id="${id}"`)) return { tag, ancestors: [...ancestors] };
  }
  assert.fail(`Missing control: ${id}`);
}

test("compact channel form keeps credentials and required fields visible while optional controls have real bounds behind a closed disclosure", () => {
  const titled: Catalog = { ...telegram, plugins: [{ ...telegram.plugins[0], schema: { ...telegram.plugins[0].schema,
    properties: { ...telegram.plugins[0].schema.properties, token: { ...telegram.plugins[0].schema.properties!.token, title: "Bot token" } },
  } }] };
  const html = renderPanel(titled);
  for (const id of ["channel-plugin", "channel-id", "channel-mainSessionId", "channel-token"])
    assert.deepEqual(fieldLocation(html, id).ancestors, [], `${id} must be visible without opening optional sections`);
  for (const id of ["channel-token_env", "channel-allowed_chat_ids", "channel-poll_timeout"]) {
    const { ancestors } = fieldLocation(html, id);
    assert.equal(ancestors.length, 1); assert.match(ancestors[0], /data-disclosure-key="channel-options:telegram"/);
    assert.doesNotMatch(ancestors[0], /\bopen(?:=|\s|>)/);
    assert.ok(html.indexOf('id="channel-token"') < html.indexOf(`id="${id}"`));
  }
  const timeout = fieldLocation(html, "channel-poll_timeout").tag;
  assert.match(timeout, /type="number"/); assert.match(timeout, /min="1"/); assert.match(timeout, /max="50"/); assert.match(timeout, /step="1"/);
  assert.match(html, /for="channel-token" title="token">Bot token/);
  assert.match(html, /for="channel-allowed_chat_ids" title="allowed_chat_ids">Allowed chat ids/);
  assert.match(html, /<code>allowed_chat_ids<\/code>/, "Actual schema keys remain inspectable");
  assert.deepEqual(fieldLocation(renderPanel(catalog), "channel-label").ancestors, [], "Required public properties stay outside More connection options");
});

test("plain factories and required complex properties keep their JSON editor available by default", () => {
  for (const schema of [{}, { properties: { required_object: { type: "object" } }, required: ["required_object"] }]) {
    const generic: Catalog = { ...telegram, plugins: [{ ...telegram.plugins[0], schema }] };
    const { ancestors } = fieldLocation(renderPanel(generic), "channel-json");
    assert.equal(ancestors.length, 1);
    assert.match(ancestors[0], /data-disclosure-key="channel-json:telegram"/);
    assert.match(ancestors[0], /\bopen=""/);
  }
});

test("Save validation reveals a hidden invalid option before focus and retains the expansion without changing unrelated choices", () => {
  const draft: ChannelDraft = { ...makeDraft(telegram, "main-root"), id: "telegram-personal" };
  draft.fields.poll_timeout = "0";
  const { errors } = buildSave(telegram, draft, sessions);
  const location = fieldLocation(renderPanel(telegram, draft, errors), "channel-poll_timeout");
  assert.match(location.tag, /aria-invalid="true"/);
  assert.doesNotMatch(location.ancestors[0], /\bopen=""/);
  const key = /data-disclosure-key="([^"]+)"/.exec(location.ancestors[0])![1];
  let choices = new Map([[key, false], ["another-plugin-options", false], ["user-opened-json", true]]);
  let focused = false;
  let target: HTMLElement | null;
  const boundary = { querySelector: (selector: string) => {
    assert.equal(selector, '[aria-invalid="true"]:not(:disabled)'); return target;
  } } as unknown as HTMLElement;
  const options = { tagName: "DETAILS", open: false, dataset: { disclosureKey: key }, parentElement: boundary } as unknown as HTMLDetailsElement;
  target = { parentElement: options, focus: () => {
    assert.equal(options.open, true, "Closed ancestors must open synchronously before focus");
    assert.equal(choices.get(key), true, "React disclosure state must retain the revealed choice"); focused = true;
  } } as unknown as HTMLElement;
  const remember = (key: string, open: boolean) => { choices = rememberDisclosure(choices, key, open); };
  assert.equal(revealInvalidField(boundary, remember), true); assert.equal(focused, true);
  assert.equal(choices.get("another-plugin-options"), false); assert.equal(choices.get("user-opened-json"), true);
  // Correcting fields does not force the optional section closed again.
  draft.fields.poll_timeout = "30"; assert.deepEqual(buildSave(telegram, draft, sessions).errors, {});
  target = null; focused = false;
  assert.equal(revealInvalidField(boundary, remember), false); assert.equal(focused, false); assert.equal(choices.get(key), true);
});
