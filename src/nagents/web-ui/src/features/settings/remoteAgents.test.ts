import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement, useState } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { AgentConfiguration } from "../team/TeamPane.js";
import { TeamTree } from "../team/TeamTree.js";
import { providerLabel, type TeamAgent } from "../team/model.js";
import { ContextIndicator } from "../context/ContextIndicator.js";
import type { ContextStats } from "../../types.js";
import { RemoteAgentEditor } from "./navigator/RemoteAgentEditor.js";
import { RemoteStatus } from "../team/RemoteStatus.js";

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
    name: "peer", description: "Remote review", remote_name: "Advertised remote name", remote_description: "Remote card purpose", metadata_observed_at: 1, metadata_status: "current",
    connection: { ...connection, client_id: clientId },
    signed_in: signedIn, authentication: { state: signedIn ? "signed_in" : clientId ? "sign_in_required" : "host_setup_required", expires_at: 0, error_code: "" },
    trust_remote_tools: false, user_invocable: true, subagent_available: true,
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
  assert.match(document.querySelector('[aria-label="Remote agent"]')?.textContent || "", /Advertised remote name/);
  assert.match(document.querySelector('[aria-label="Remote agent"]')?.textContent || "", /Remote card purpose/);
  assert.match(document.querySelector('[aria-label="Remote agent"]')?.textContent || "", /From remote server/);
  assert.equal((document.querySelector('input[placeholder="Optional description for this workspace"]') as HTMLInputElement | null)?.placeholder, "Optional description for this workspace");
  assert.equal((document.querySelector('input[value="Remote review"]') as HTMLInputElement | null)?.value, "Remote review");
});

test("unused remote definitions remain visible with connection access without an empty TeamTree group", () => {
  const html = renderToStaticMarkup(createElement(TeamTree, {
    agents: [],
    profiles: [{ id: "peer", name: "peer-alias", description: "Local note", remote_name: "Advertised name",
      remote_description: "Advertised purpose", metadata_status: "refresh_failed", provider: "a2a", model: "", used: false, available: true }],
    selected: "", inspect() {}, configure() {}, arrivals: {}, error: "", refresh() {},
  }));
  assert.match(html, /data-profile-id="peer"/);
  assert.match(html, /<strong>Advertised name<\/strong>/);
  assert.match(html, /Not started/);
  assert.match(html, /remote-connection-chip/);
  assert.match(html, /title="Connection: peer/);
  assert.match(html, /Settings for Advertised name definition/);
  assert.doesNotMatch(html, /team-definition-group/);
  assert.match(html, /Advertised purpose/);
  assert.doesNotMatch(html, /Local note/);
  assert.doesNotMatch(html, /team-start-agent|team-run-search|<select/);
});

test("remote auth states explain server credential availability without exposing credential values", () => {
  const statuses = [
    [{ state: "signed_in", expires_at: 0, error_code: "", refreshable: true }, "Signed in · auto refresh"],
    [{ state: "signed_in", expires_at: 1, error_code: "" }, "Sign-in required"],
    [{ state: "not_required", expires_at: 0, error_code: "" }, "No sign-in required"],
    [{ state: "token_missing", expires_at: 0, error_code: "" }, "Server token missing"],
    [{ state: "token_ready", expires_at: 0, error_code: "" }, "Server token configured"],
    [{ state: "host_setup_required", expires_at: 0, error_code: "" }, "Sign-in setup required"],
  ] as const;
  for (const [authentication, label] of statuses) {
    const html = renderToStaticMarkup(createElement(RemoteStatus, { authentication }));
    assert.ok(html.includes(label), `${authentication.state} should render its status`);
    assert.doesNotMatch(html, /access_token|refresh_token|secret-value|bearer/i);
  }
});

test("remote trust can be saved and revoked, and focus polling refreshes authentication status", async t => {
  let trusted = false;
  let signedIn = false;
  const trustBodies: { revision: string; trusted: boolean }[] = [];
  const currentCatalog = () => {
    const result = catalog(signedIn);
    result.agents[0].trust_remote_tools = trusted;
    return result;
  };
  const ui = await renderEditor("peer", async (path, init) => {
    if (path === "/api/remote-agents/peer/trust" && init?.method === "PUT") {
      const body = JSON.parse(String(init.body)) as { revision: string; trusted: boolean };
      trustBodies.push(body); trusted = body.trusted;
      return Response.json(currentCatalog());
    }
    if (path === "/api/remote-agents") return Response.json(currentCatalog());
    throw new Error(`Unexpected request ${path}`);
  });
  t.after(async () => ui.restore());
  const document = ui.dom.window.document;
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
  const trustButton = () => [...document.querySelectorAll<HTMLButtonElement>("button")]
    .find(button => /Trust all remote tools|Require tool review/.test(button.textContent || ""))!;
  await act(async () => { trustButton().click(); await new Promise(resolve => setTimeout(resolve, 0)); });
  assert.deepEqual(trustBodies[0], { revision: "revision-1", trusted: true });
  assert.equal(trustButton().getAttribute("aria-pressed"), "true");

  signedIn = true;
  await act(async () => { ui.dom.window.dispatchEvent(new ui.dom.window.Event("focus")); await new Promise(resolve => setTimeout(resolve, 0)); });
  assert.match(document.querySelector(".remote-auth-status")?.textContent || "", /Signed in/);

  await act(async () => { trustButton().click(); await new Promise(resolve => setTimeout(resolve, 0)); });
  assert.deepEqual(trustBodies[1], { revision: "revision-1", trusted: false });
  assert.equal(trustButton().getAttribute("aria-pressed"), "false");
});

test("a stale focus poll cannot overwrite a newer saved trust decision", async t => {
  let trusted = false;
  let deferNextStatus = false;
  let resolveStale!: (response: Response) => void;
  const trustBodies: { revision: string; trusted: boolean }[] = [];
  const ui = await renderEditor("peer", async (path, init) => {
    if (path === "/api/remote-agents" && deferNextStatus) {
      deferNextStatus = false;
      return await new Promise<Response>(resolve => { resolveStale = resolve; });
    }
    if (path === "/api/remote-agents") {
      const response = catalog(); response.agents[0].trust_remote_tools = trusted;
      return Response.json(response);
    }
    if (path === "/api/remote-agents/peer/trust" && init?.method === "PUT") {
      const body = JSON.parse(String(init.body)) as { revision: string; trusted: boolean };
      trustBodies.push(body); trusted = body.trusted;
      const response = catalog(); response.agents[0].trust_remote_tools = trusted;
      return Response.json(response);
    }
    throw new Error(`Unexpected request ${path}`);
  });
  t.after(async () => ui.restore());
  const document = ui.dom.window.document;
  await act(async () => { await new Promise(resolve => setTimeout(resolve, 0)); });
  deferNextStatus = true;
  await act(async () => {
    ui.dom.window.dispatchEvent(new ui.dom.window.Event("focus"));
    await Promise.resolve(); await Promise.resolve();
  });
  assert.equal(typeof resolveStale, "function", "focus starts a deferred status read");

  const trustButton = [...document.querySelectorAll<HTMLButtonElement>("button")]
    .find(button => button.textContent?.includes("Trust all remote tools"))!;
  await act(async () => { trustButton.click(); await new Promise(resolve => setTimeout(resolve, 0)); });
  assert.deepEqual(trustBodies, [{ revision: "revision-1", trusted: true }]);
  assert.equal(trustButton.getAttribute("aria-pressed"), "true");

  await act(async () => {
    const stale = catalog(); stale.agents[0].trust_remote_tools = false;
    resolveStale(Response.json(stale));
    await new Promise(resolve => setTimeout(resolve, 0));
  });
  const currentButton = [...document.querySelectorAll<HTMLButtonElement>("button")]
    .find(button => button.textContent?.includes("Require tool review"));
  assert.ok(currentButton, "the resolved stale response cannot undo the newer saved trust state");
  assert.equal(currentButton.getAttribute("aria-pressed"), "true");
});

test("a rejected remote trust update leaves the connection untrusted and shows the server error", async t => {
  const ui = await renderEditor("peer", async path => {
    if (path === "/api/remote-agents") return Response.json(catalog());
    if (path === "/api/remote-agents/peer/trust") return Response.json({ detail: "Connection changed; reload settings" }, { status: 409 });
    throw new Error(`Unexpected request ${path}`);
  });
  t.after(async () => ui.restore());
  const button = [...ui.dom.window.document.querySelectorAll<HTMLButtonElement>("button")]
    .find(element => element.textContent?.includes("Trust all remote tools"))!;
  await act(async () => { button.click(); await new Promise(resolve => setTimeout(resolve, 0)); });
  assert.equal(button.getAttribute("aria-pressed"), "false");
  assert.match(ui.dom.window.document.querySelector('[role="alert"]')?.textContent || "", /Connection changed/);
});

test("new remote agent name pattern is valid under HTML v-mode rules", async t => {
  const ui = await renderEditor("", async () => Response.json({ revision: "revision-1", source_path: "/workspace/connections/a2a.json", agents: [] }));
  t.after(async () => ui.restore());
  const document = ui.dom.window.document;
  const details = document.querySelector("details")!;
  details.open = true;
  const input = document.querySelector<HTMLInputElement>('input[placeholder="Use the remote agent’s name"]')!;
  const pattern = input.getAttribute("pattern");
  assert.ok(pattern);
  const validName = new RegExp(`^(?:${pattern})$`, "v");
  for (const name of ["peer", "Mini-Agent_2", "A".repeat(128)]) assert.match(name, validName);
  for (const name of ["has space", "slash/name", "[brackets]", "A".repeat(129)]) assert.doesNotMatch(name, validName);
});

test("missing public client shows the exact callback and host setup guidance", async t => {
  const ui = await renderEditor("peer", async () => Response.json(catalog(false, "")));
  t.after(async () => ui.restore());
  const document = ui.dom.window.document;
  assert.match(document.body.textContent || "", /Sign-in setup required/);
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
  assert.match(ui.dom.window.document.body.textContent || "", /Signed in\. Saved for this user across chats and workspaces using the same connection\./);
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
  assert.match(document.body.textContent || "", /Signed in\. Saved for this user across chats and workspaces using the same connection\./, requests.join("\n"));
  assert.equal([...document.querySelectorAll("button")].some(button => button.textContent?.trim() === "Sign in again"), true);
});
