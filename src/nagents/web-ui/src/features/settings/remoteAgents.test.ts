import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, useState } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { AgentConfiguration } from "../team/TeamPane.js";
import { providerLabel, type TeamAgent } from "../team/model.js";
import { ContextIndicator } from "../context/ContextIndicator.js";
import type { ContextStats } from "../../types.js";
import { RemoteAgentEditor } from "./navigator/RemoteAgentEditor.js";

type RemoteConnection = {
  url: string; rpc_url: string; issuer: string; client_id: string; scopes: string[];
  resource: string; token_env: string; ca_file: string; loopback_http: boolean; timeout: number; metadata_url: string;
};
const connection: RemoteConnection = {
  url: "https://peer.example", rpc_url: "https://peer.example/rpc", issuer: "https://id.example/realm",
  client_id: "public-web", scopes: ["openid", "profile"], resource: "", token_env: "", ca_file: "",
  loopback_http: false, timeout: 120, metadata_url: "https://id.example/realm/.well-known/openid-configuration",
};
function catalog(signedIn = false, clientId = connection.client_id) {
  return { revision: "revision-1", source_path: "/workspace/connections/a2a.json", agents: [{
    name: "peer", description: "Remote review", connection: { ...connection, client_id: clientId },
    signed_in: signedIn, user_invocable: true, subagent_available: true,
  }] };
}
function browser() {
  const dom = new JSDOM("<div id='root'></div>", { url: "https://ngn.example.test/settings" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  const globals = { window: dom.window, document: dom.window.document, HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true, fetch: globalThis.fetch };
  for (const [key, value] of Object.entries(globals)) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  const root = createRoot(document.getElementById("root")!);
  return { dom, root, async restore() {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of Object.keys(globals)) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  } };
}
async function renderEditor(agent: string, fetcher: (path: string, init?: RequestInit) => Response | Promise<Response>) {
  const ui = browser();
  Object.defineProperty(globalThis, "fetch", { configurable: true, writable: true, value: fetcher });
  await act(async () => {
    ui.root.render(createElement(RemoteAgentEditor, { agent, token: "local-token", saved() {} }));
    await Promise.resolve(); await Promise.resolve();
  });
  return ui;
}
const remoteContext: ContextStats = {
  components: [], total_tokens: 0, context_window: null, context_window_source: "provider", remaining_tokens: null,
  observed_prompt_tokens: null, observed_completion_tokens: null, observed_reasoning_tokens: null,
  observed_reasoning_in_output: "unknown", observed_context_tokens: null, pending_context_tokens: 0,
  context_tokens: 0, context_tokens_source: "provider", provider: "a2a", model: "", estimate_method: "remote",
};

test("remote editor exposes discovered metadata read-only without manual OAuth fields", async t => {
  const ui = await renderEditor("peer", async path => {
    assert.equal(path, "/api/remote-agents"); return Response.json(catalog());
  });
  t.after(async () => ui.restore());
  const document = ui.dom.window.document;
  const labels = [...document.querySelectorAll("label")].map(label => label.textContent || "").join(" ");
  assert.doesNotMatch(labels, /issuer|client ID|scopes/i);
  const details = document.querySelector("details")!; details.open = true;
  assert.match(details.textContent || "", /Sign-in service · advertised by host/);
  assert.match(details.textContent || "", /https:\/\/id\.example\/realm/);
  assert.match(details.textContent || "", /public-web/);
  assert.match(details.textContent || "", /openid profile/);
  assert.equal(document.querySelector('input[aria-label="Issuer"]'), null);
  assert.equal(document.querySelector('input[aria-label="Client ID"]'), null);
  assert.equal(document.querySelector('input[aria-label="Scopes"]'), null);
});

test("missing public client shows the exact callback and host setup guidance", async t => {
  const ui = await renderEditor("peer", async () => Response.json(catalog(false, "")));
  t.after(async () => ui.restore());
  const document = ui.dom.window.document;
  assert.match(document.body.textContent || "", /Host sign-in setup required/);
  assert.match(document.body.textContent || "", /administrator must configure the client/);
  assert.match(document.body.textContent || "", /https:\/\/ngn\.example\.test\/remote-agent-login\/callback/);
  const signIn = [...document.querySelectorAll("button")].find(button => button.textContent?.includes("Sign in"));
  assert.ok(signIn?.disabled);
  assert.doesNotMatch([...document.querySelectorAll("label")].map(label => label.textContent).join(" "), /issuer|client ID|scopes/i);
});

test("sign-in opens the advertised URL and status polling reflects the saved grant", async t => {
  let signedIn = false;
  const ui = await renderEditor("peer", async (path, init) => {
    if (path === "/api/remote-agents") return Response.json(catalog(signedIn));
    if (path === "/api/remote-agents/peer/login" && init?.method === "POST")
      return Response.json({ authorization_url: "https://id.example/authorize?state=fixture" });
    throw new Error(`Unexpected request ${path}`);
  });
  t.after(async () => ui.restore());
  const opened = { opener: {} as unknown, location: { href: "about:blank" }, close() {} };
  Object.defineProperty(ui.dom.window, "open", { configurable: true, value: () => opened });
  const intervals: (() => void)[] = [];
  Object.defineProperty(ui.dom.window, "setInterval", { configurable: true, value: (callback: () => void) => { intervals.push(callback); return intervals.length; } });
  Object.defineProperty(ui.dom.window, "setTimeout", { configurable: true, value: () => 1 });
  const button = [...ui.dom.window.document.querySelectorAll("button")].find(element => element.textContent?.trim() === "Sign in")!;
  await act(async () => { button.click(); await Promise.resolve(); await Promise.resolve(); });
  assert.equal(opened.opener, null);
  assert.equal(opened.location.href, "https://id.example/authorize?state=fixture");
  assert.match(ui.dom.window.document.body.textContent || "", /Complete sign-in, then return here/);
  assert.ok(intervals.length);
  signedIn = true;
  await act(async () => { intervals.forEach(callback => callback()); await new Promise(resolve => setTimeout(resolve, 0)); });
  assert.match(ui.dom.window.document.body.textContent || "", /Signed in\. You can now use this remote agent\./);
  assert.equal([...ui.dom.window.document.querySelectorAll("button")].some(element => element.textContent?.trim() === "Sign in again"), true);
});

test("A2A task settings have no local model controls and the context panel reports remote ownership", () => {
  const agent: TeamAgent = { id: "task-1", name: "Remote peer", profile: "peer", parentId: "", prompt: "task", phase: "ready", reason: "",
    provider: "", providerKind: "a2a", model: "", startedAt: 0, updatedAt: 0, canMessage: false, messageReason: "" };
  const settings = renderToStaticMarkup(createElement(AgentConfiguration, { agent, token: "", sessionId: "session-1", changed() {}, close() {} }));
  assert.match(settings, /This conversation runs on the remote A2A server/);
  assert.match(settings, /Its model, instructions, tools and context are managed there/);
  assert.doesNotMatch(settings, /Next activation|Reasoning effort|Compaction|<input|<select/);
  const context = renderToStaticMarkup(createElement(ContextIndicator, { stats: remoteContext, error: "", open: false, setOpen() {} }));
  assert.match(context, /Remote context/);
  assert.doesNotMatch(context, /0 tokens|Context usage|token window/);
  assert.equal(providerLabel(agent), "Remote A2A");
});


test("URL-only connect keeps popup sign-in pending when saved selection changes from new to named", async t => {
  const ui = browser();
  t.after(async () => ui.restore());
  let stored = false;
  let signedIn = false;
  const requests: string[] = [];
  const discoveredConnection = { ...connection, client_id: "public-web" };
  const namedCatalog = (isSignedIn: boolean) => {
    const reply = catalog(isSignedIn, "public-web");
    reply.agents[0].name = "peer-agent";
    return reply;
  };
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    writable: true,
    value: async (input: string | URL, init?: RequestInit) => {
      const path = String(input); requests.push(`${init?.method || "GET"} ${path}`);
      if (path === "/api/remote-agents" && !stored) return Response.json({ revision: "revision-1", agents: [], source_path: "/workspace/a2a.json" });
      if (path === "/api/remote-agents" && stored) return Response.json(namedCatalog(signedIn));
      if (path === "/api/remote-agents/discover" && init?.method === "POST") {
        return Response.json({ connection: discoveredConnection, name: "Peer Agent", description: "Remote peer", authentication: "oauth" });
      }
      if (path === "/api/remote-agents/peer-agent" && init?.method === "PUT") {
        stored = true;
        return Response.json(namedCatalog(false));
      }
      if (path === "/api/remote-agents/peer-agent/login" && init?.method === "POST") {
        return Response.json({ authorization_url: "https://id.example/authorize?state=new-agent" });
      }
      throw new Error(`Unexpected request ${path}`);
    },
  });
  const callbacks: (() => void)[] = [];
  Object.defineProperty(ui.dom.window, "open", {
    configurable: true, value: () => ({ opener: {}, location: { href: "about:blank" }, close() {} }),
  });
  Object.defineProperty(ui.dom.window, "setInterval", {
    configurable: true, value: (callback: () => void) => { callbacks.push(callback); return callbacks.length; },
  });
  Object.defineProperty(ui.dom.window, "setTimeout", { configurable: true, value: () => 1 });
  function NewAgentHost() {
    const [agent, setAgent] = useState("");
    return createElement(RemoteAgentEditor, { agent, token: "local-token", saved: setAgent });
  }
  await act(async () => {
    ui.root.render(createElement(NewAgentHost));
    await Promise.resolve(); await Promise.resolve();
  });
  const document = ui.dom.window.document;
  const urlInput = document.querySelector<HTMLInputElement>('input[type="url"]')!;
  const setter = Object.getOwnPropertyDescriptor(ui.dom.window.HTMLInputElement.prototype, "value")!.set!;
  await act(async () => {
    setter.call(urlInput, "https://peer.example");
    urlInput.dispatchEvent(new ui.dom.window.Event("input", { bubbles: true }));
    urlInput.dispatchEvent(new ui.dom.window.Event("change", { bubbles: true }));
  });
  const form = document.querySelector("form")!;
  await act(async () => {
    form.dispatchEvent(new ui.dom.window.Event("submit", { bubbles: true, cancelable: true }));
    await new Promise(resolve => setTimeout(resolve, 0));
  });
  assert.equal(document.querySelector("h3")?.textContent, "Remote connection");
  assert.match(document.body.textContent || "", /Connected\. Available to the next assistant round without restarting\./);
  assert.ok(callbacks.length, "the pending status poll survives changing the selected agent prop");
  signedIn = true;
  await act(async () => { callbacks.forEach(callback => callback()); await new Promise(resolve => setTimeout(resolve, 50)); });
  assert.match(document.body.textContent || "", /Signed in\. You can now use this remote agent\./, requests.join("\n"));
  assert.equal([...document.querySelectorAll("button")].some(button => button.textContent?.trim() === "Sign in again"), true);
});
