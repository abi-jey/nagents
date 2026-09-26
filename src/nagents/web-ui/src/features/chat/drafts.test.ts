import assert from "node:assert/strict";
import test from "node:test";
import { SessionDrafts } from "./drafts.js";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { useSessionDraft } from "./useSessionDraft.js";

test("unsent text remains with its conversation through switching and soft deletion/restore", () => {
  const drafts = new SessionDrafts();
  drafts.set("first", "Unsent first conversation");
  assert.equal(drafts.get("new").text, "");
  drafts.set("new", "Unsent second conversation");
  // Soft deletion changes session membership, not the tab-local draft's identity.
  assert.equal(drafts.get("first").text, "Unsent first conversation");
  drafts.forget("first");
  assert.equal(drafts.get("first").text, "");
  assert.equal(drafts.get("new").text, "Unsent second conversation");
});

test("an acknowledgement only clears its unchanged submitted draft, including while viewing another root", () => {
  const drafts = new SessionDrafts();
  drafts.set("first", "Submitted message");
  const submitted = drafts.get("first");
  drafts.set("second", "Keep this text");
  drafts.submitted("first", submitted, "Submitted message");
  assert.equal(drafts.get("first").text, "");
  assert.equal(drafts.get("second").text, "Keep this text");
});

test("typing during admission survives late success, even if the user returns to identical text", () => {
  const drafts = new SessionDrafts();
  drafts.set("root", "same text");
  const submitted = drafts.get("root");
  drafts.set("root", "new text");
  drafts.set("root", "same text");
  drafts.submitted("root", submitted, "same text");
  assert.equal(drafts.get("root").text, "same text");
  drafts.forget("root");
  drafts.set("root", "same text");
  drafts.submitted("root", submitted, "same text");
  assert.equal(drafts.get("root").text, "same text");
});

test("starter prompts do not erase a different unsent draft", () => {
  const drafts = new SessionDrafts();
  drafts.set("root", "My own question");
  drafts.submitted("root", drafts.get("root"), "Show me this workspace");
  assert.equal(drafts.get("root").text, "My own question");
});

test("mounted draft subscriptions follow selection immediately and retain newer typing during acknowledgement", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  let editor: ReturnType<typeof useSessionDraft>;
  function Editor({ sessionId }: { sessionId: string }) {
    editor = useSessionDraft(sessionId);
    return createElement("textarea", { value: editor.prompt, readOnly: true });
  }
  const render = async (sessionId: string) => act(async () => root.render(createElement(Editor, { sessionId })));
  const value = () => container.querySelector("textarea")!.value;
  try {
    await render("first");
    await act(async () => editor.setPrompt("draft A"));
    await render("second");
    assert.equal(value(), "");
    await act(async () => editor.setPrompt("draft B"));
    await render("first");
    assert.equal(value(), "draft A");
    const sent = editor!.drafts.get("first");
    await act(async () => editor.setPrompt("newer draft A"));
    await act(async () => editor.drafts.submitted("first", sent, "draft A"));
    assert.equal(value(), "newer draft A");
    await render("second");
    assert.equal(value(), "draft B");
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});
