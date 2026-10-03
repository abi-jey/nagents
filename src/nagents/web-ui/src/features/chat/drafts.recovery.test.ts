import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { SessionDrafts } from "./drafts.js";
import { recoverOrphanDraft } from "./draftRecovery.js";
import { DraftRecoveryNotice } from "./DraftRecoveryNotice.js";
import { useSessionDraft } from "./useSessionDraft.js";
import type { Snapshot } from "../../types.js";
import { deferred } from "../../api/testFixtures.js";

const blank = (id = "ngn-new"): Snapshot => ({
  session_id: id, sessions: [{ id, title: "", updated_at: "2026-10-03" }], history: [], retained_tasks: [], active_run: null,
});

test("catalog removal exposes every nonempty orphan draft, retaining normal and restored drafts by identity", () => {
  const drafts = new SessionDrafts();
  drafts.set("ngn-pruned", "First unsent message"); drafts.set("ngn-other-pruned", "Another unsent message");
  drafts.set("ngn-live", "Current draft"); drafts.set("ngn-empty", ""); drafts.set("ngn-space", "  ");
  assert.deepEqual(drafts.orphaned(["ngn-live"]).map((draft) => draft.sessionId), ["ngn-pruned", "ngn-other-pruned", "ngn-space"]);
  assert.deepEqual(drafts.orphaned(["ngn-live", "ngn-pruned", "ngn-space"]).map((draft) => draft.sessionId), ["ngn-other-pruned"]);
  assert.equal(drafts.get("ngn-pruned").text, "First unsent message", "restoring membership does not erase the original draft");
});

test("recovery opens one confirmed blank conversation and moves exact unsent text without a submission", async () => {
  const drafts = new SessionDrafts(), original = "First line\n\n<script>Unsent text & quotes</script>\n ";
  drafts.set("ngn-pruned", original);
  const snapshot = blank(); const actions: string[] = [];
  await recoverOrphanDraft("ngn-pruned", { drafts,
    openBlank: async () => { actions.push("new"); return snapshot; }, selected: () => snapshot.session_id,
    load: (value) => { assert.equal(value, snapshot); actions.push("load"); },
  });
  assert.deepEqual(actions, ["new", "load"]);
  assert.equal(drafts.get(snapshot.session_id).text, original); assert.equal(drafts.get("ngn-pruned").text, "");
  assert.deepEqual(drafts.orphaned([snapshot.session_id]), []);
});

for (const existing of ["Different unsent text", "Original text", " "]) test(`a reused blank root with ${JSON.stringify(existing)} never overwrites either draft`, async () => {
  const drafts = new SessionDrafts(); drafts.set("ngn-pruned", "Original text"); drafts.set("ngn-new", existing);
  const originalRevision = drafts.get("ngn-pruned").revision, targetRevision = drafts.get("ngn-new").revision;
  await assert.rejects(recoverOrphanDraft("ngn-pruned", {
    drafts, openBlank: async () => blank(), selected: () => "ngn-new", load: () => {},
  }), /Both drafts are kept.*conversation with saved messages/);
  assert.equal(drafts.get("ngn-pruned").text, "Original text"); assert.equal(drafts.get("ngn-new").text, existing);
  assert.equal(drafts.get("ngn-pruned").revision, originalRevision); assert.equal(drafts.get("ngn-new").revision, targetRevision);
});

test("failed or unconfirmed creation retains source text and never transfers into a different selection", async () => {
  for (const response of [undefined, { ...blank(), sessions: [] }, { ...blank(), sessions: [{ ...blank().sessions[0], parent_session_id: "parent" }] }]) {
    const drafts = new SessionDrafts(); drafts.set("ngn-pruned", "Keep me"); let loads = 0;
    await assert.rejects(recoverOrphanDraft("ngn-pruned", { drafts, openBlank: async () => response,
      selected: () => "ngn-new", load: () => { loads++; },
    }), /could not be confirmed/);
    assert.equal(drafts.get("ngn-pruned").text, "Keep me"); assert.equal(drafts.get("ngn-new").text, ""); assert.equal(loads, 0);
  }
  const drafts = new SessionDrafts(); drafts.set("ngn-pruned", "Keep me");
  await assert.rejects(recoverOrphanDraft("ngn-pruned", { drafts,
    openBlank: async () => { throw new Error("Network unavailable"); }, selected: () => "ngn-new", load: () => assert.fail("No history accepted"),
  }), /Network unavailable/);
  await assert.rejects(recoverOrphanDraft("ngn-pruned", { drafts,
    openBlank: async () => blank(), selected: () => "ngn-selected-elsewhere", load: () => assert.fail("No history accepted"),
  }), /could not be confirmed/);
  assert.equal(drafts.get("ngn-pruned").text, "Keep me"); assert.equal(drafts.get("ngn-new").text, "");
});

test("recovery refuses model history or active/retained work even when New returns the selected root", async () => {
  const used = [
    { ...blank(), history: [{ role: "user", content: "Saved conversation", name: "", tool_call_id: "", tool_calls: [] }] },
    { ...blank(), active_run: { id: "working", status: "running" } },
    { ...blank(), retained_tasks: [{ id: "retained" }] } as Snapshot,
  ];
  for (const snapshot of used) {
    const drafts = new SessionDrafts(); drafts.set("ngn-pruned", "Keep the source");
    await assert.rejects(recoverOrphanDraft("ngn-pruned", {
      drafts, openBlank: async () => snapshot, selected: () => "ngn-new", load: () => {},
    }), /not blank/);
    assert.equal(drafts.get("ngn-pruned").text, "Keep the source"); assert.equal(drafts.get("ngn-new").text, "");
  }
});

test("recovery uses the latest source typing and refuses destination typing that arrives during New", async () => {
  const drafts = new SessionDrafts(); drafts.set("ngn-pruned", "First version");
  const pending = deferred<Snapshot>();
  const recovering = recoverOrphanDraft("ngn-pruned", { drafts, openBlank: () => pending.promise, selected: () => "ngn-new", load: () => {} });
  drafts.set("ngn-pruned", "Latest source text"); pending.resolve(blank()); await recovering;
  assert.equal(drafts.get("ngn-new").text, "Latest source text");
  drafts.set("ngn-another-pruned", "Second source");
  const pendingAgain = deferred<Snapshot>();
  const conflict = recoverOrphanDraft("ngn-another-pruned", { drafts, openBlank: () => pendingAgain.promise, selected: () => "ngn-next", load: () => {} });
  drafts.set("ngn-next", "Typing into the destination"); pendingAgain.resolve(blank("ngn-next"));
  await assert.rejects(conflict, /Both drafts are kept/);
  assert.equal(drafts.get("ngn-another-pruned").text, "Second source"); assert.equal(drafts.get("ngn-next").text, "Typing into the destination");
});

test("late acknowledgements cannot clear recovered text and missing sources never create new roots", async () => {
  const drafts = new SessionDrafts(); drafts.set("ngn-pruned", "Same text");
  const sourceSubmission = drafts.get("ngn-pruned");
  drafts.set("ngn-new", "Same text"); const targetSubmission = drafts.get("ngn-new"); drafts.set("ngn-new", "");
  await recoverOrphanDraft("ngn-pruned", { drafts, openBlank: async () => blank(), selected: () => "ngn-new", load: () => {} });
  drafts.submitted("ngn-pruned", sourceSubmission, "Same text"); drafts.submitted("ngn-new", targetSubmission, "Same text");
  assert.equal(drafts.get("ngn-new").text, "Same text");
  await assert.rejects(recoverOrphanDraft("ngn-pruned", { drafts,
    openBlank: async () => { assert.fail("No duplicate New request"); }, selected: () => "ngn-new", load: () => {},
  }), /no longer available/);
});

test("a mounted composer exposes lost drafts read-only and reacts to recovery or forgetting an offscreen root", async () => {
  const dom = new JSDOM("<div id='root'></div>"), previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!, root = createRoot(container);
  let editor!: ReturnType<typeof useSessionDraft>;
  function Editor({ sessionId, available, disabled = false }: { sessionId: string; available: string[]; disabled?: boolean }) {
    editor = useSessionDraft(sessionId);
    return createElement("div", {}, createElement("textarea", { "aria-label": "Current draft", value: editor.prompt, readOnly: true }),
      createElement(DraftRecoveryNotice, { drafts: editor.drafts.orphaned(available), disabled,
        recover: (id) => { editor.drafts.moveToEmpty(id, sessionId); },
      }));
  }
  const render = async (sessionId: string, available: string[], disabled = false) => act(async () => root.render(createElement(Editor, { sessionId, available, disabled })));
  try {
    await render("ngn-old", ["ngn-old"]); await act(async () => editor.setPrompt("Unsent <script>text</script>\nSecond line"));
    await render("ngn-new", ["ngn-new"], true);
    const original = container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Original unsent draft 1"]')!;
    assert.equal(original.value, "Unsent <script>text</script>\nSecond line"); assert.equal(original.readOnly, true); assert.equal(original.disabled, false);
    assert.equal(container.querySelector("script"), null); assert.equal(container.querySelector<HTMLButtonElement>("button")!.disabled, true);
    await render("ngn-new", ["ngn-new"]);
    await act(async () => container.querySelector<HTMLButtonElement>("button")!.click());
    assert.equal(container.querySelector<HTMLTextAreaElement>('textarea[aria-label="Current draft"]')!.value, "Unsent <script>text</script>\nSecond line");
    assert.equal(container.querySelector(".draft-recovery"), null);
    await act(async () => editor.drafts.set("ngn-orphan", "Another recovery"));
    assert.ok(container.querySelector(".draft-recovery"));
    await act(async () => editor.drafts.forget("ngn-orphan"));
    assert.equal(container.querySelector(".draft-recovery"), null, "offscreen store changes notify the current composer");
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]); else Reflect.deleteProperty(globalThis, key);
    }
  }
});
