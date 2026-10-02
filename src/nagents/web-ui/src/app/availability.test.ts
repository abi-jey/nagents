import assert from "node:assert/strict";
import test from "node:test";
import { availability } from "./availability.js";

const idle = { ready: true, operating: false, running: false,
  modal: false, approval: false, uploadsReady: true };

test("a running conversation permits follow-ups, navigation and voice while idle-only changes wait", () => {
  const access = availability({ ...idle, running: true });
  assert.equal(access.submit, true);
  assert.equal(access.navigate, true);
  assert.equal(access.channels, true);
  assert.equal(access.create, false);
  assert.equal(access.settings, false);
  assert.equal(access.designer, false);
  assert.equal(access.live, true);
});

test("open panels, approvals and pending mutations block both command and navigation affordances", () => {
  for (const running of [false, true])
    for (const blocking of [{ modal: true }, { approval: true }, { operating: true }, { ready: false }])
      assert.ok(Object.values(availability({ ...idle, running, ...blocking })).every((value) => !value));
});

test("uploads not ready prevent submission but allow navigation and Trash", () => {
  const uploading = availability({ ...idle, uploadsReady: false });
  assert.equal(uploading.submit, false);
  assert.equal(uploading.navigate, true);
  assert.equal(uploading.trash, true);
});
