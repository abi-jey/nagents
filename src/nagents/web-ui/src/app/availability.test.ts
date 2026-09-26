import assert from "node:assert/strict";
import test from "node:test";
import { availability } from "./availability.js";

const idle = { ready: true, operating: false, running: false, dictating: false,
  reviewing: false, modal: false, approval: false, uploadsReady: true };

test("a running conversation permits follow-ups and navigation while idle-only changes wait", () => {
  const access = availability({ ...idle, running: true });
  assert.equal(access.submit, true);
  assert.equal(access.navigate, true);
  assert.equal(access.channels, true);
  assert.equal(access.create, false);
  assert.equal(access.settings, false);
  assert.equal(access.designer, false);
  assert.equal(access.live, false);
});

test("open panels, approvals and pending mutations block both command and navigation affordances", () => {
  for (const blocking of [{ modal: true }, { approval: true }, { operating: true }, { ready: false }])
    assert.ok(Object.values(availability({ ...idle, ...blocking })).every((value) => !value));
});

test("dictation review can inspect Trash without enabling navigation or submission", () => {
  const reviewing = availability({ ...idle, dictating: true, reviewing: true });
  assert.equal(reviewing.trash, true);
  assert.equal(reviewing.navigate, false);
  assert.equal(reviewing.submit, false);
  assert.equal(reviewing.live, false);
  assert.equal(availability({ ...idle, dictating: true }).trash, false);
  const uploading = availability({ ...idle, uploadsReady: false });
  assert.equal(uploading.submit, false);
  assert.equal(uploading.navigate, true);
});
