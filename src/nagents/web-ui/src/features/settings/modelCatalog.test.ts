import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { RequestError } from "../../api/client.js";
import { createDraft, parseDraft } from "./draft.js";
import { ModelField } from "./ModelField.js";
import {
  filterModels,
  modelCatalogFailure,
  modelCatalogStatus,
  readModelCatalog,
  type ModelCatalogState,
} from "./modelCatalog.js";

const idle: ModelCatalogState = { loading: false, attempted: false, error: "" };
const catalog = {
  models: ["vendor/custom:latest", "another-ID", "<model>&\"quoted\"", "future model"],
  source: "codex",
};

test("catalog discovery uses only the shared local GET transport and supplied abort signal", async (t) => {
  const controller = new AbortController();
  const fetchMock = t.mock.method(globalThis, "fetch", async (input: string, init: RequestInit) => {
    assert.equal(input, "/api/models");
    assert.equal(init.method, "GET");
    assert.deepEqual(init.headers, { "X-Ngn-Token": "local-test-token" });
    assert.equal(init.signal, controller.signal);
    assert.equal(init.body, undefined);
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.cache, "no-store");
    return Response.json(catalog);
  });
  assert.deepEqual(await readModelCatalog("local-test-token", controller.signal), catalog);
  assert.equal(fetchMock.mock.callCount(), 1);
});

test("catalog IDs and active source are preserved without guessing capabilities or vendor prefixes", async (t) => {
  const reply = { ...catalog, source: "openai_compatible", models: [...catalog.models, "another-ID"] };
  t.mock.method(globalThis, "fetch", async () => Response.json(reply));
  assert.deepEqual(await readModelCatalog("", new AbortController().signal), reply);
});

test("an empty catalog is a successful response with a manual-entry explanation", async (t) => {
  t.mock.method(globalThis, "fetch", async () => Response.json({ models: [], source: "new_provider" }));
  const empty = await readModelCatalog("", new AbortController().signal);
  assert.deepEqual(empty, { models: [], source: "new_provider" });
  const status = modelCatalogStatus({ ...idle, attempted: true, catalog: empty });
  assert.match(status, /0 model IDs fetched\. Source: new_provider\./);
  assert.match(status, /returned no model IDs/);
  assert.match(status, /manually/);
});

test("malformed catalog responses fail without inventing fallback IDs", async (t) => {
  for (const response of [null, [], {}, { models: [] }, { models: "model", source: "codex" }, { models: [1], source: "codex" }, { models: [{}], source: "codex" }, { models: [null], source: "codex" }, { models: [""], source: "codex" }, { models: ["  "], source: "codex" }, { models: [], source: 1 }, { models: [], source: " " }]) {
    const mock = t.mock.method(globalThis, "fetch", async () => Response.json(response));
    await assert.rejects(readModelCatalog("", new AbortController().signal), SyntaxError);
    mock.mock.restore();
  }
  t.mock.method(globalThis, "fetch", async () => new Response("not JSON"));
  await assert.rejects(readModelCatalog("", new AbortController().signal), SyntaxError);
});

test("catalog statuses distinguish explicit loading, success, active source, and stale refresh results", () => {
  assert.match(modelCatalogStatus(idle), /not been fetched/);
  assert.match(modelCatalogStatus({ ...idle, loading: true, attempted: true }), /Fetching models/);
  const success = { ...idle, attempted: true, catalog };
  assert.match(modelCatalogStatus(success), /4 model IDs fetched\. Source: codex\./);
  assert.match(modelCatalogStatus(success), /then Save to apply/);
  assert.match(modelCatalogStatus({ ...success, loading: true }), /Refreshing models.*Previous results may be stale/);
  assert.match(modelCatalogStatus({ ...success, error: "Refresh failed." }), /Refresh failed.*Source: codex.*previous results; they may be stale.*save manually/);
  assert.match(modelCatalogStatus({ ...success, catalog: { models: ["one"], source: "active_source" } }), /1 model ID fetched\. Source: active_source\./);
});

for (const status of [501, 502]) {
  test(`${status} shows the safe server detail, permits manual entry, and does not retry`, async (t) => {
    const detail = status === 501 ? "Discovery is unavailable for this provider." : "Unable to load provider models.";
    const mock = t.mock.method(globalThis, "fetch", async () => Response.json({ detail }, { status }));
    await assert.rejects(readModelCatalog("", new AbortController().signal), (cause: unknown) => {
      assert.ok(cause instanceof RequestError);
      assert.equal(cause.status, status);
      const error = modelCatalogFailure(cause);
      assert.ok(error.includes(detail));
      assert.match(error, status === 501 ? /not supported/ : /Could not fetch/);
      assert.match(modelCatalogStatus({ ...idle, attempted: true, error }), /save manually/);
      return true;
    });
    assert.equal(mock.mock.callCount(), 1);
  });
}

test("network and protocol failures are separate from settings revision errors", () => {
  assert.match(modelCatalogFailure(new TypeError("Failed to fetch")), /Check the connection/);
  assert.match(modelCatalogFailure(new SyntaxError("Unexpected JSON")), /invalid model catalog/);
  assert.match(modelCatalogFailure("unexpected"), /Could not reach/);
  assert.doesNotMatch(modelCatalogStatus({ ...idle, error: modelCatalogFailure(new RequestError("Busy", 409)) }), /Refresh settings|outcome is unknown/);
});

test("search is case-insensitive text matching, preserves exact IDs, and never changes the catalog", () => {
  assert.deepEqual(filterModels(catalog.models, " VENDOR/ "), ["vendor/custom:latest"]);
  assert.deepEqual(filterModels(catalog.models, "model"), ["<model>&\"quoted\"", "future model"]);
  assert.deepEqual(filterModels(catalog.models, "not listed"), []);
  assert.deepEqual(filterModels(catalog.models, ""), catalog.models);
  assert.equal(catalog.models.length, 4);
});

test("manual models outside the catalog remain valid without changing other draft fields", () => {
  const draft = createDraft({ model: "manual/not-in-catalog", agent: "build", shell_timeout: 30, max_output: 16384, max_file_bytes: 1048576, max_tool_rounds: 37, max_subagent_depth: 2, dictation_enabled: false, dictation_model: "gpt-4o-mini-transcribe", dictation_language: "", dictation_max_seconds: 60 });
  const profiles = [{ name: "build", mode: "build" as const, model: "" }];
  assert.equal(filterModels(catalog.models, draft.model).length, 0);
  assert.ok(parseDraft(draft, profiles).ok);
  assert.equal(draft.max_tool_rounds, "37");
  assert.equal(draft.model, "manual/not-in-catalog");
});

test("the model field renders accessible manual entry without fetching or updating the draft on mount", (t) => {
  const fetchMock = t.mock.method(globalThis, "fetch", async () => { throw new Error("Unexpected fetch"); });
  const html = renderToStaticMarkup(createElement(ModelField, {
    token: "local-test-token",
    model: "manual/<model>&",
    error: "Model validation message.",
    update: () => assert.fail("Rendering must not select a model"),
  }));
  assert.match(html, /<label for="settings-model">Model ID<\/label>/);
  assert.match(html, /value="manual\/&lt;model&gt;&amp;"/);
  assert.match(html, /aria-describedby="settings-model-help settings-model-error"/);
  assert.match(html, /<button type="button"[^>]*>Fetch models<\/button>/);
  assert.match(html, /role="status" aria-live="polite"/);
  assert.match(html, /Model validation message\./);
  assert.doesNotMatch(html, /<model>|settings-model-search|<li>/);
  assert.equal(fetchMock.mock.callCount(), 0);
});

test("aborting discovery propagates cancellation without retrying", async (t) => {
  const controller = new AbortController();
  const mock = t.mock.method(globalThis, "fetch", async (_input: string, init: RequestInit) => {
    return new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => reject(init.signal?.reason), { once: true });
    });
  });
  const pending = readModelCatalog("", controller.signal);
  controller.abort();
  await assert.rejects(pending, { name: "AbortError" });
  assert.equal(mock.mock.callCount(), 1);
});

test("a late response from a cancelled read cannot replace a newer catalog", async (t) => {
  const old = new AbortController();
  let finish!: (response: Response) => void;
  const mock = t.mock.method(globalThis, "fetch", async () => new Promise<Response>((resolve) => { finish = resolve; }));
  const pending = readModelCatalog("", old.signal);
  old.abort();
  mock.mock.restore();
  t.mock.method(globalThis, "fetch", async () => Response.json({ models: ["new-model"], source: "new-source" }));
  const replacement = await readModelCatalog("", new AbortController().signal);
  finish(Response.json(catalog));
  await assert.rejects(pending, { name: "AbortError" });
  assert.deepEqual(replacement, { models: ["new-model"], source: "new-source" });
});
