import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { parseFolders } from "./sessionGroups.js";
import { chatBranches, folderOptions, selectedPath, sessionTree } from "../features/sessions/sessionTree.js";
import { SessionFolders } from "../features/sessions/SessionFolders.js";
import type { Session } from "../types.js";

const group = (index: number, parent = "") => ({ id: `group-${index.toString(16).padStart(32, "0")}`, name: `Folder ${index}`, collapsed: true, parent_id: parent });
const a = group(1), b = group(2, a.id), c = group(3, b.id);
const revision = "f".repeat(32);
const session = (id: string, forked_from = ""): Session => ({ id, title: id, updated_at: "today", forked_from });

test("folder wire parsing accepts old roots, rejects dangling parents, cycles and excessive depth", () => {
  assert.equal(parseFolders({ revision, groups: [{ id: a.id, name: a.name, collapsed: false }], memberships: {} }).groups[0].parent_id, "");
  assert.deepEqual(parseFolders({ revision, groups: [c, a, b], memberships: {} }).groups, [c, a, b]);
  const deep = [a];
  for (let index = 2; index <= 13; index++) deep.push(group(index, deep.at(-1)!.id));
  for (const groups of [[b], [{ ...a, parent_id: a.id }], [{ ...a, parent_id: b.id }, b], deep])
    assert.throws(() => parseFolders({ revision, groups, memberships: {} }), /Invalid chat folders/);
  assert.equal(parseFolders({ revision, groups: deep.slice(0,12), memberships: {} }).groups.length, 12);
});

test("nested folder search keeps ancestor paths and fork ancestry without revealing unrelated chats", () => {
  const sessions = [session("source"), session("matching leaf", "source"), session("unrelated")];
  const memberships = { source: c.id, "matching leaf": c.id, unrelated: a.id };
  const tree = sessionTree(sessions, [c, a, b], memberships, "matching", "matching leaf");
  assert.equal(tree.folders[0].folder.id, a.id);
  assert.equal(tree.folders[0].containsCurrent, true);
  assert.equal(tree.folders[0].count, 2);
  assert.equal(tree.folders[0].chats.length, 0);
  const leaf = tree.folders[0].folders[0].folders[0];
  assert.equal(leaf.chats[0].session.id, "source");
  assert.equal(leaf.chats[0].children[0].session.id, "matching leaf");
  assert.equal(sessionTree(sessions, [a,b,c], memberships, "folder 2").folders[0].count, 2);
});

test("moved, trashed or malformed fork ancestors never hide a used chat", () => {
  const sessions = [session("source"), session("fork", "source"), session("grandchild", "fork")];
  const together = chatBranches(sessions, {}, "");
  assert.equal(together.length, 1); assert.equal(together[0].children[0].children[0].session.id, "grandchild");
  const moved = chatBranches(sessions, { source: a.id }, "");
  assert.equal(moved[0].session.id, "fork"); assert.equal(moved[0].origin, "source");
  assert.equal(moved[0].children[0].session.id, "grandchild");
  const missing = chatBranches(sessions.slice(1), {}, "");
  assert.equal(missing[0].origin, "unavailable chat");
  const cycle = chatBranches([session("one", "two"), session("two", "one"), session("three", "one")], {}, "");
  assert.deepEqual(cycle.map(branch => branch.session.id), ["one", "two", "three"]);
});

test("move-folder choices distinguish paths and exclude the entire moving subtree", () => {
  assert.deepEqual(folderOptions([c,b,a]).map(option => option.label), ["Folder 1", "Folder 1 / Folder 2", "Folder 1 / Folder 2 / Folder 3"]);
  assert.deepEqual(folderOptions([a,b,c], b.id).map(option => option.id), [a.id]);
});

test("selection reveals only its visible branch and folder ancestry after a move", () => {
  const sessions = [session("source"), session("fork", "source"), session("leaf", "fork")];
  const membership = { source: a.id, fork: c.id, leaf: c.id };
  assert.deepEqual(selectedPath(sessions, [a,b,c], membership, "leaf"), { forkIds: ["fork"], folderIds: [c.id,b.id,a.id] });
});

test("very long fork lineages remain visible with bounded rendering depth", () => {
  const sessions = Array.from({ length: 100 }, (_, index) => session(`chat-${index}`, index ? `chat-${index-1}` : ""));
  const tree = chatBranches(sessions, {}, "");
  function flatten(nodes: typeof tree, depth = 1): string[] {
    assert.ok(!nodes.length || depth <= 64);
    return nodes.flatMap(node => [node.session.id, ...flatten(node.children, depth + 1)]);
  }
  assert.equal(new Set(flatten(tree)).size, 100);
});

test("fork collapse is local to a workspace and nested rows remain mounted", async () => {
  const dom = new JSDOM("<div id='root'></div>", { url: "http://localhost" });
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, localStorage: dom.window.localStorage, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  const render = (workspace: string, selected = "source") => act(async () => root.render(createElement(SessionFolders, {
    sessions: [session("source"), session("fork", "source")], selected, workspace,
    renderSession: item => createElement("button", { "data-session": item.id }, item.title),
  })));
  try {
    await render("first");
    await act(async () => container.querySelector<HTMLButtonElement>(".branch-toggle")!.click());
    assert.equal(container.querySelector("#forks-source")!.hasAttribute("hidden"), true);
    assert.ok(container.querySelector('[data-session="fork"]'));
    assert.deepEqual(JSON.parse(dom.window.localStorage.getItem("ngn.fork-branches:first")!), ["source"]);
    await render("second"); assert.equal(container.querySelector("#forks-source")!.hasAttribute("hidden"), false);
    await render("first"); assert.equal(container.querySelector("#forks-source")!.hasAttribute("hidden"), true);
    await render("first", "fork"); assert.equal(container.querySelector("#forks-source")!.hasAttribute("hidden"), false);
    await act(async () => container.querySelector<HTMLButtonElement>(".branch-toggle")!.click());
    await render("first", "fork"); assert.equal(container.querySelector("#forks-source")!.hasAttribute("hidden"), true,
      "An explicit later collapse is respected until selection changes");
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const name of ["window", "document", "localStorage", "IS_REACT_ACT_ENVIRONMENT"])
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
  }
});
