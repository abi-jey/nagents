import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { parseFolders } from "./sessionGroups.js";
import { SessionFolders } from "../features/sessions/SessionFolders.js";
import { SessionMenu } from "../features/sessions/SessionMenu.js";
import type { ChatFolderActions } from "../features/sessions/useChatFolders.js";

const id = "group-" + "a".repeat(32);
const revision = "b".repeat(32);

test("folder wire data preserves unicode names and explicit server membership", () => {
  const value = parseFolders({ revision, groups: [{ id, name: "Research 🔬", collapsed: true }], memberships: { "ngn-one": id } });
  assert.equal(value.groups[0].name, "Research 🔬");
  assert.equal(value.groups[0].collapsed, true);
  assert.equal(value.memberships["ngn-one"], id);
  assert.equal(Object.getPrototypeOf(value.memberships), null);
});

test("invalid folder identities, unknown memberships and duplicate IDs are rejected", () => {
  const valid = { revision, groups: [{ id, name: "Research", collapsed: false }], memberships: { "ngn-one": id } };
  for (const changed of [
    { revision: "" }, { groups: [{ ...valid.groups[0], id: "../../other" }] },
    { groups: [...valid.groups, ...valid.groups] }, { groups: [{ ...valid.groups[0], collapsed: "false" }] },
    { groups: [{ ...valid.groups[0], name: " " }] }, { memberships: { "ngn-one": "group-" + "c".repeat(32) } },
    { memberships: { "../foreign": id } },
  ]) assert.throws(() => parseFolders({ ...valid, ...changed }), /Invalid chat folders/);
});

test("folder view keeps collapsed chats and ungrouped chats separate and escapes names", () => {
  const name = '<img src=x onerror="alert(1)">';
  const value = parseFolders({ revision, groups: [{ id, name, collapsed: true }], memberships: { "ngn-one": id } });
  const actions: ChatFolderActions = {
    value, error: "", pending: false, ready: true, clearError() {}, async refresh() {},
    async create() { return true; }, async update() { return true; }, async move() { return true; }, async remove() { return true; }, async reparent() { return true; },
  };
  const html = renderToStaticMarkup(createElement(SessionFolders, {
    folders: actions, selected: "ngn-one", sessions: [
      { id: "ngn-one", title: "Grouped chat", updated_at: "today" },
      { id: "ngn-two", title: "Unsorted chat", updated_at: "today" },
    ],
    renderSession: (session) => createElement("div", { key: session.id }, session.title),
  }));
  const dom = new JSDOM(html);
  const document = dom.window.document;
  assert.equal(document.querySelector("img"), null);
  assert.ok(document.querySelector(".folder-toggle")?.textContent?.includes(name));
  assert.equal(document.querySelector(".folder-toggle")?.getAttribute("aria-expanded"), "false");
  assert.equal(document.querySelector(`#folder-${id}`)?.hasAttribute("hidden"), true);
  assert.ok(document.querySelector(`#folder-${id}`)?.textContent?.includes("Grouped chat"));
  assert.equal(document.querySelector('[aria-label="Ungrouped chats"] li')?.textContent, "Unsorted chat");
  assert.equal(document.querySelectorAll('[aria-label="Search chats and folders"]').length, 1);
  dom.window.close();
});

test("a running chat can be moved without enabling its destructive actions", () => {
  const html = renderToStaticMarkup(createElement(SessionMenu, {
    session: { id: "ngn-one", title: "Running", updated_at: "today", active_run_id: "active" },
    disabled: true, permanent() { assert.fail("Must not delete"); }, move() {},
  }));
  const dom = new JSDOM(html);
  const buttons = [...dom.window.document.querySelectorAll("button")];
  assert.equal(buttons.find(button => button.getAttribute("aria-haspopup") === "menu")?.disabled, false);
  assert.equal(buttons.find(button => button.textContent?.includes("Move to folder"))?.disabled, false);
  assert.equal(buttons.find(button => button.textContent?.includes("Delete forever"))?.disabled, true);
  dom.window.close();
});
