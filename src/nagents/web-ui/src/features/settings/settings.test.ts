import assert from "node:assert/strict";
import test from "node:test";
import { RequestError } from "../../api/client.js";
import { createDraft, parseDraft, selectProfile } from "./draft.js";
import { readSettings, resetSettings, saveSettings, settingsFailure } from "./transport.js";
import type { SettingsReply, SettingsValues } from "./types.js";
import { dictationConfig } from "../dictation/testFixtures.js";

const values: SettingsValues = {
  model: "startup-model",
  agent: "build",
  shell_timeout: 30,
  max_output: 16384,
  max_file_bytes: 1048576,
  max_tool_rounds: 100,
  max_subagent_depth: 2,
  dictation_enabled: false,
  dictation_model: "gpt-4o-mini-transcribe",
  dictation_language: "",
  dictation_max_seconds: 60,
};
const reply: SettingsReply = {
  values,
  defaults: values,
  profiles: [
    { name: "build", mode: "build", model: "" },
    { name: "review", mode: "reviewer", model: "review-model" },
  ],
  revision: 'opaque/revision:01+"not-a-counter"',
  persisted: false,
  effective_mode: "build",
  connection: { provider: "mock", api: "responses", auth_status: "configured" },
  dictation: dictationConfig,
};

test("settings drafts round-trip exact API keys and numeric values", () => {
  const parsed = parseDraft(createDraft(values), reply.profiles);
  assert.deepEqual(parsed, { ok: true, values });
});

test("model IDs are trimmed without inventing or restricting model choices", () => {
  const parsed = parseDraft({ ...createDraft(values), model: "  vendor/custom-model:latest  " }, reply.profiles);
  assert.equal(parsed.ok && parsed.values.model, "vendor/custom-model:latest");
});

test("model IDs reject blanks, excess length, and control characters even at the edges", () => {
  for (const model of ["", "   ", "x".repeat(201), "\nmodel", "model\t", "mo\u0000del", "model\u007f", "mo\u0085del"]) {
    const parsed = parseDraft({ ...createDraft(values), model }, reply.profiles);
    assert.ok(!parsed.ok && parsed.errors.model, JSON.stringify(model));
  }
  for (const model of ["x".repeat(200), "\u{1f680}".repeat(200)]) {
    assert.ok(parseDraft({ ...createDraft(values), model }, reply.profiles).ok);
  }
});

test("profiles come from the server and only nonempty profile models preset the draft", () => {
  const draft = createDraft(values);
  const selected = selectProfile(draft, "review", reply.profiles);
  assert.equal(selected.agent, "review");
  assert.equal(selected.model, "review-model");
  assert.equal(draft.agent, "build");
  const overridden = { ...selected, model: "explicit-model" };
  const parsed = parseDraft(overridden, reply.profiles);
  assert.equal(parsed.ok && parsed.values.model, "explicit-model");
  assert.equal(selectProfile(overridden, "build", reply.profiles).model, "explicit-model");
  assert.equal(selectProfile(draft, "invented", reply.profiles), draft);
  assert.equal(parseDraft({ ...draft, agent: "invented" }, reply.profiles).ok, false);
  assert.equal(parseDraft(draft, []).ok, false);
});

test("shell timeout accepts positive decimals and exponents through 600 seconds", () => {
  for (const shell_timeout of ["0.001", ".5", "30.25", "600", "600.0", "1e-7", "1E+2", "6e2"]) {
    const parsed = parseDraft({ ...createDraft(values), shell_timeout }, reply.profiles);
    assert.equal(parsed.ok && parsed.values.shell_timeout, Number(shell_timeout));
  }
});

test("a saved decimal timeout remains valid for a later model-only save", () => {
  const saved = parseDraft(
    { ...createDraft(values), shell_timeout: "0.0000001" },
    reply.profiles,
  );
  assert.ok(saved.ok);
  const returned = createDraft(saved.values);
  assert.equal(returned.shell_timeout, "1e-7");
  assert.deepEqual(parseDraft(returned, reply.profiles), saved);
  assert.deepEqual(
    parseDraft({ ...returned, model: "changed-model" }, reply.profiles),
    { ok: true, values: { ...saved.values, model: "changed-model" } },
  );
});

test("backend timeout values round-trip including the smallest positive number", () => {
  for (const shell_timeout of [Number.MIN_VALUE, 1e-308, 1e-7, 1e-6, 0.5, 600]) {
    const returned = { ...values, shell_timeout };
    assert.deepEqual(parseDraft(createDraft(returned), reply.profiles), {
      ok: true,
      values: returned,
    });
  }
});

test("shell timeout rejects zero, out-of-range, and malformed numeric input", () => {
  for (const shell_timeout of ["", " ", "0", "-1", "600.01", "Infinity", "NaN", "0x20", "30s", "1,5", "+3", "1e", "1e+", "1e--2", "1e2.5", "6.001e2", "1e999", "1e-999", "0e2", "-1e-7"]) {
    const parsed = parseDraft({ ...createDraft(values), shell_timeout }, reply.profiles);
    assert.ok(!parsed.ok && parsed.errors.shell_timeout, shell_timeout);
  }
});

for (const [key, min, max] of [
  ["max_output", 1024, 1048576],
  ["max_file_bytes", 1024, 4194304],
  ["max_tool_rounds", 1, 1000],
  ["max_subagent_depth", 0, 8],
] as const) {
  test(`${key} accepts inclusive integer limits without converting bytes to tokens`, () => {
    for (const value of [min, max]) {
      const parsed = parseDraft({ ...createDraft(values), [key]: String(value) }, reply.profiles);
      assert.equal(parsed.ok && parsed.values[key], value);
    }
  });
  test(`${key} rejects out-of-range, fractional, partial, and coercible input`, () => {
    for (const value of [String(min - 1), String(max + 1), "", " ", `${min}.0`, `${min}.5`, `${min}bytes`, "1e3", "0x400", "Infinity", "NaN", "+1024", "1,024", "9007199254740993"]) {
      const parsed = parseDraft({ ...createDraft(values), [key]: value }, reply.profiles);
      assert.ok(!parsed.ok && parsed.errors[key], value);
    }
  });
}

test("invalid fields are reported together and a corrected draft validates", () => {
  const draft = { ...createDraft(values), model: "", max_output: "10", max_tool_rounds: "1.5" };
  const parsed = parseDraft(draft, reply.profiles);
  assert.ok(!parsed.ok);
  assert.deepEqual(Object.keys(parsed.errors), ["model", "max_output", "max_tool_rounds"]);
  assert.ok(parseDraft(createDraft(values), reply.profiles).ok);
});

test("GET settings uses the shared token transport and a cancellable read", async (t) => {
  const controller = new AbortController();
  const fetchMock = t.mock.method(globalThis, "fetch", async (input: string, init: RequestInit) => {
    assert.equal(input, "/api/settings");
    assert.equal(init.method, "GET");
    assert.deepEqual(init.headers, { "X-Ngn-Token": "mock-token" });
    assert.equal(init.signal, controller.signal);
    assert.equal(init.body, undefined);
    assert.equal(init.credentials, "same-origin");
    assert.equal(init.cache, "no-store");
    return Response.json(reply);
  });
  assert.deepEqual(await readSettings("mock-token", controller.signal), reply);
  assert.equal(fetchMock.mock.callCount(), 1);
});

test("save sends all exact values and the opaque revision, without a cancellable write", async (t) => {
  const fetchMock = t.mock.method(globalThis, "fetch", async (input: string, init: RequestInit) => {
    assert.equal(input, "/api/settings");
    assert.equal(init.method, "POST");
    assert.deepEqual(init.headers, { "X-Ngn-Token": "mock-token", "Content-Type": "application/json" });
    assert.deepEqual(JSON.parse(String(init.body)), { revision: reply.revision, values });
    assert.equal(init.signal, undefined);
    return Response.json({ ...reply, persisted: true, revision: "new-opaque-revision" });
  });
  const saved = await saveSettings("mock-token", reply.revision, values);
  assert.equal(saved.revision, "new-opaque-revision");
  assert.equal(saved.persisted, true);
  assert.equal(fetchMock.mock.callCount(), 1);
});

test("reset sends only the expected revision and trusts the returned startup defaults", async (t) => {
  const fetchMock = t.mock.method(globalThis, "fetch", async (input: string, init: RequestInit) => {
    assert.equal(input, "/api/settings/reset");
    assert.equal(init.method, "POST");
    assert.deepEqual(JSON.parse(String(init.body)), { revision: reply.revision });
    assert.equal(init.signal, undefined);
    return Response.json(reply);
  });
  assert.deepEqual(await resetSettings("mock-token", reply.revision), reply);
  assert.equal(fetchMock.mock.callCount(), 1);
});

for (const detail of ["Settings changed in another connection.", "A run is active."]) {
  test(`409 is not retried and requires an explicit refresh: ${detail}`, async (t) => {
    const fetchMock = t.mock.method(globalThis, "fetch", async () => Response.json({ detail }, { status: 409 }));
    await assert.rejects(saveSettings("mock-token", reply.revision, values), (cause: unknown) => {
      assert.ok(cause instanceof RequestError);
      assert.equal(cause.status, 409);
      assert.equal(cause.message, detail);
      const failure = settingsFailure(cause, true);
      assert.equal(failure.needsRefresh, true);
      assert.ok(failure.message.includes(detail));
      assert.match(failure.message, /draft is kept/);
      assert.match(failure.message, /No request was retried/);
      return true;
    });
    assert.equal(fetchMock.mock.callCount(), 1);
  });
}

test("a lost or server-failed write has an uncertain outcome and must be refreshed", () => {
  for (const cause of [new TypeError("Failed to fetch"), new SyntaxError("Invalid JSON"), new RequestError("Server failed", 500)]) {
    const failure = settingsFailure(cause, true);
    assert.equal(failure.needsRefresh, true);
    assert.match(failure.message, /outcome is unknown/);
  }
  assert.equal(settingsFailure(new TypeError("Failed to fetch"), false).needsRefresh, false);
});

test("safe server validation errors remain actionable without a forced draft discard", () => {
  assert.deepEqual(settingsFailure(new RequestError("Choose a valid profile.", 400), true), {
    message: "Choose a valid profile.",
    needsRefresh: false,
  });
});

test("non-JSON HTTP failures retain their status", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("Unavailable", { status: 503 }));
  await assert.rejects(resetSettings("mock-token", reply.revision), (cause: unknown) => {
    assert.ok(cause instanceof RequestError);
    assert.equal(cause.status, 503);
    assert.match(cause.message, /503/);
    return true;
  });
});
