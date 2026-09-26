import assert from "node:assert/strict";
import test from "node:test";
import { setImmediate } from "node:timers/promises";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { JSDOM } from "jsdom";
import { UploadDraft } from "./uploads.js";
import { Composer } from "./Composer.js";
import { fromHistory } from "./transcript.js";
import { LiveSessions } from "./liveTranscript.js";
import { MessageQueue, queuedMessageFailure } from "../../api/messages.js";

test("uploads only stage drafts; an uncertain submission preserves references until confirmed", async (t) => {
  const requests: string[] = [];
  t.mock.method(URL, "createObjectURL", () => "blob:draft");
  const revoked = t.mock.method(URL, "revokeObjectURL", () => {});
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    requests.push(path);
    assert.ok(!path.includes("/messages"));
    if (init.method === "GET") return Response.json({ file_media_types: ["image/png", "application/pdf"] });
    if (init.method === "DELETE") return Response.json({ removed: true });
    const file = init.body as File;
    return Response.json({ upload_id: path.split("/").at(-1), session_id: "root", filename: file.name,
      media_type: file.type, byte_length: file.size, expires_at: Date.now() / 1000 + 3600 });
  });
  const draft = new UploadDraft();
  draft.configure("token", "root"); await setImmediate();
  await draft.add([new File(["png"], "picture.png", { type: "image/png" })]);
  assert.equal(draft.getSnapshot().items[0].status, "ready");
  const ids = draft.begin();
  draft.remove(ids[0]); assert.equal(draft.getSnapshot().items.length, 1, "Submission locks attachment mutation");
  draft.finish(ids, false); assert.deepEqual(draft.begin(), ids);
  draft.finish(ids, true); assert.equal(draft.getSnapshot().items.length, 0);
  assert.equal(revoked.mock.callCount(), 1);
  assert.equal(requests.filter((path) => path.includes(ids[0])).length, 1, "Confirmed bytes are retained, not deleted");
  draft.dispose();
});

test("navigation aborts uploads, revokes previews and discards even a late noncooperative acknowledgement", async (t) => {
  let finish!: (response: Response) => void;
  let signal: AbortSignal | undefined;
  const deletes: string[] = [];
  t.mock.method(URL, "createObjectURL", () => "blob:pending");
  const revoke = t.mock.method(URL, "revokeObjectURL", () => {});
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    if (init.method === "GET") return Response.json({ file_media_types: ["image/png"] });
    if (init.method === "DELETE") { deletes.push(path); return Response.json({ removed: true }); }
    signal = init.signal || undefined;
    return new Promise<Response>((resolve) => { finish = resolve; });
  });
  const draft = new UploadDraft(); draft.configure("token", "old-root"); await setImmediate();
  const adding = draft.add([new File(["png"], "image.png", { type: "image/png" })]);
  const id = draft.getSnapshot().items[0].id;
  draft.configure("token", "new-root");
  assert.ok(signal?.aborted); assert.equal(revoke.mock.callCount(), 1);
  finish(Response.json({ upload_id: id, session_id: "old-root", media_type: "image/png", byte_length: 3, expires_at: Date.now() / 1000 + 10 }));
  await adding;
  assert.equal(draft.getSnapshot().items.length, 0);
  assert.ok(deletes.every((path) => path === `/api/sessions/old-root/uploads/${id}`));
  assert.equal(deletes.length, 2);
  draft.dispose();
});

test("provider types, size and count bounds reject before fetch; failed/expired drafts cannot submit", async (t) => {
  let posts = 0;
  t.mock.method(URL, "createObjectURL", () => "blob:image"); t.mock.method(URL, "revokeObjectURL", () => {});
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    if (init.method === "GET") return Response.json({ file_media_types: ["image/png"] });
    if (init.method === "DELETE") return Response.json({});
    posts++;
    if (posts === 1) return Response.json({ detail: "Rejected file" }, { status: 415 });
    return Response.json({ upload_id: path.split("/").at(-1), session_id: "root", media_type: "image/png", byte_length: 3, expires_at: 1 });
  });
  const draft = new UploadDraft(); draft.configure("token", "root"); await setImmediate();
  await draft.add([new File(["pdf"], "doc.pdf", { type: "application/pdf" }), new File([new Uint8Array(8 * 1024 * 1024 + 1)], "big.png", { type: "image/png" })]);
  assert.equal(posts, 0);
  await draft.add([new File(["png"], "a.png", { type: "image/png" })]);
  assert.throws(() => draft.begin(), /failed/);
  draft.remove(draft.getSnapshot().items[0].id);
  await draft.add(Array.from({ length: 4 }, () => new File(["png"], "a.png", { type: "image/png" })));
  assert.equal(draft.getSnapshot().items.length, 3);
  assert.equal(posts, 4);
  assert.throws(() => draft.begin(), /expired/);
  draft.dispose();
});

test("message retry identity includes ordered attachments and image-only history keeps metadata", () => {
  const queue = new MessageQueue(); let id = 0;
  const uuid = () => String(++id);
  const first = queue.prepare("root", "", uuid, ["one", "two"]);
  assert.equal(queue.prepare("root", "", uuid, ["one", "two"]), first);
  assert.notEqual(queue.prepare("root", "", uuid, ["two", "one"]).message_id, first.message_id);
  assert.notEqual(queue.prepare("root", "", uuid, ["one"]).message_id, first.message_id);
  assert.equal(queuedMessageFailure("", "root", ["a".repeat(66000)]).length > 0, true);
  const entries = fromHistory({ session_id: "root", retained_tasks: [], history: [{ role: "user", content: "", name: "", tool_call_id: "", tool_calls: [],
    uploads: [{ upload_id: "one", filename: "image.png", media_type: "image/png", byte_length: 3 }],
  }] });
  assert.equal(entries.length, 1); assert.equal(entries[0].kind, "user"); assert.equal(entries[0].uploads?.[0].filename, "image.png");
  const cache = new LiveSessions();
  cache.enqueue(first);
  assert.equal(cache.reject(first).entries.length, 0);
});

test("an abandoned file batch cannot resume after navigating away and back", async (t) => {
  let finish!: (response: Response) => void;
  let posts = 0;
  t.mock.method(URL, "createObjectURL", () => "blob:draft"); t.mock.method(URL, "revokeObjectURL", () => {});
  t.mock.method(globalThis, "fetch", async (_: string, init: RequestInit) => {
    if (init.method === "GET") return Response.json({ file_media_types: ["image/png"] });
    if (init.method === "DELETE") return Response.json({});
    posts++;
    return new Promise<Response>((resolve) => { finish = resolve; });
  });
  const draft = new UploadDraft(); draft.configure("token", "root"); await setImmediate();
  const operation = draft.add([new File(["png"], "one.png", { type: "image/png" }), new File(["png"], "two.png", { type: "image/png" })]);
  const id = draft.getSnapshot().items[0].id;
  draft.configure("token", "other"); draft.configure("token", "root"); await setImmediate();
  finish(Response.json({ upload_id: id, session_id: "root", media_type: "image/png", byte_length: 3, expires_at: Date.now() / 1000 + 10 }));
  await operation;
  assert.equal(posts, 1); assert.equal(draft.getSnapshot().items.length, 0);
  draft.dispose();
});

test("an unconfirmed send can retry its immutable identity after the original staging deadline", async (t) => {
  const started = Date.now();
  t.mock.method(URL, "createObjectURL", () => "blob:draft"); t.mock.method(URL, "revokeObjectURL", () => {});
  t.mock.method(globalThis, "fetch", async (path: string, init: RequestInit) => {
    if (init.method === "GET") return Response.json({ file_media_types: ["image/png"] });
    return Response.json({ upload_id: path.split("/").at(-1), session_id: "root", media_type: "image/png", byte_length: 3, expires_at: started / 1000 + 10 });
  });
  const draft = new UploadDraft(); draft.configure("token", "root"); await setImmediate();
  await draft.add([new File(["png"], "image.png", { type: "image/png" })]);
  const ids = draft.begin(); draft.finish(ids, false);
  t.mock.method(Date, "now", () => started + 20_000);
  assert.deepEqual(draft.begin(), ids);
  draft.finish(ids, true); draft.dispose();
});

test("paste, drop and picker only add drafts; image-only Send requires an explicit action", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const before = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true,
    ResizeObserver: class { observe() {} disconnect() {} } });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container); let added = 0, sent = 0;
  const file = new File(["image"], "picture.png", { type: "image/png" });
  const props = { inputRef: () => {}, prompt: "", setPrompt: () => {}, demo: false, disabled: false, canSubmit: true, running: false,
    submit: () => { sent++; }, cancel: () => {}, attachmentTypes: ["image/png"], addAttachments: (files: File[]) => { added += files.length; } };
  try {
    await act(async () => root.render(createElement(Composer, props)));
    assert.equal(container.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled, true);
    for (const type of ["paste", "drop"]) {
      const event = new dom.window.Event(type, { bubbles: true, cancelable: true });
      Object.defineProperty(event, type === "paste" ? "clipboardData" : "dataTransfer", { value: { files: [file], types: ["Files"] } });
      await act(async () => container.querySelector("textarea")!.dispatchEvent(event));
      assert.equal(event.defaultPrevented, true); assert.equal(sent, 0);
    }
    const picker = container.querySelector<HTMLInputElement>('input[type="file"]')!;
    Object.defineProperty(picker, "files", { value: [file] });
    await act(async () => picker.dispatchEvent(new dom.window.Event("change", { bubbles: true })));
    assert.equal(added, 3); assert.equal(sent, 0);
    await act(async () => root.render(createElement(Composer, { ...props, hasAttachments: true })));
    await act(async () => container.querySelector<HTMLButtonElement>('button[type="submit"]')!.click());
    assert.equal(sent, 1);
    await act(async () => root.render(createElement(Composer, {
      ...props, prompt: "Next draft while sending", canSubmit: false, submitting: true,
      attachmentsDisabled: true, running: true, stopping: true,
    })));
    assert.equal(container.querySelector("textarea")!.disabled, false, "Admission does not take away the editor");
    assert.equal(container.querySelector("textarea")!.value, "Next draft while sending");
    assert.equal(container.querySelector<HTMLInputElement>('input[type="file"]')!.disabled, true);
    assert.equal(container.querySelector('button[type="submit"]')!.textContent, "Sending…");
    assert.equal(container.querySelector<HTMLButtonElement>("button.cancel")!.disabled, true);
    const drop = new dom.window.Event("drop", { bubbles: true, cancelable: true });
    Object.defineProperty(drop, "dataTransfer", { value: { files: [file], types: ["Files"] } });
    await act(async () => container.querySelector("textarea")!.dispatchEvent(drop));
    await act(async () => container.querySelector("form")!.dispatchEvent(new dom.window.Event("submit", { bubbles: true, cancelable: true })));
    assert.equal(added, 3, "The attachment set stays immutable during admission");
    assert.equal(sent, 1, "Pending admission cannot be submitted twice");
  } finally {
    await act(async () => root.unmount()); dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT", "ResizeObserver"]) {
      if (before[key]) Object.defineProperty(globalThis, key, before[key]); else Reflect.deleteProperty(globalThis, key);
    }
  }
});
