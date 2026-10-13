import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { MessageContent } from "./MessageContent.js";
import { Conversation } from "./Conversation.js";
import { appendEvent, fromHistory, type Entry } from "./transcript.js";
import { taskEntries } from "../team/TaskConversation.js";
import type { TaskDetail } from "../team/model.js";

function render(text: string) {
  return new JSDOM(renderToStaticMarkup(createElement(MessageContent, { text }))).window.document;
}

test("assistant Markdown renders headings, emphasis, nested lists, quotes, rules and GFM tables", () => {
  const doc = render("# Summary\n\n**Ready** with *care* and ~~old~~ `flags`.\n\n"
    + "3. Deploy\n   - Preview\n   - Publish\n4. Verify\n\n> Keep the original.\n\n---\n\n"
    + "| Feature | State |\n| :--- | ---: |\n| **Chat** | Ready |\n| Escaped \\| pipe | `a\\|b` |\n");
  assert.equal(doc.querySelector("h1")?.textContent, "Summary");
  assert.equal(doc.querySelector("p strong")?.textContent, "Ready");
  assert.equal(doc.querySelector("em")?.textContent, "care");
  assert.equal(doc.querySelector("del")?.textContent, "old");
  assert.equal(doc.querySelector("ol")?.start, 3);
  assert.deepEqual([...doc.querySelectorAll("ol > li:first-child > ul > li")].map(li => li.textContent), ["Preview", "Publish"]);
  assert.equal(doc.querySelector("blockquote p")?.textContent, "Keep the original.");
  assert.equal(doc.querySelectorAll("hr").length, 1);
  assert.deepEqual([...doc.querySelectorAll("th")].map(th => [th.textContent, th.scope, th.style.textAlign]),
    [["Feature", "col", "left"], ["State", "col", "right"]]);
  assert.equal(doc.querySelectorAll("tbody tr").length, 2);
  assert.equal(doc.querySelector("tbody strong")?.textContent, "Chat");
  assert.equal(doc.querySelector("tbody tr:last-child td")?.textContent, "Escaped | pipe");
  assert.equal(doc.querySelector("tbody code")?.textContent, "a|b");
  const scroller = doc.querySelector("table")!.parentElement!;
  assert.equal(scroller.getAttribute("role"), "region");
  assert.equal(scroller.getAttribute("aria-label"), "Table");
  assert.equal(scroller.tabIndex, 0);
});

test("loose and task lists have semantic items and exactly one noninteractive checkbox per task", () => {
  const doc = render("- [x] **Shipped**\n\n  Reviewed by a person.\n\n- [ ] Pending\n  - A nested item\n");
  const items = doc.querySelectorAll(".markdown-content > ul > li");
  assert.equal(items.length, 2);
  assert.equal(items[0].querySelectorAll("p").length, 2);
  const boxes = [...doc.querySelectorAll<HTMLInputElement>("input")];
  assert.equal(boxes.length, 2);
  assert.ok(boxes.every(box => box.type === "checkbox" && box.disabled));
  assert.deepEqual(boxes.map(box => box.checked), [true, false]);
  assert.deepEqual(boxes.map(box => box.getAttribute("aria-label")), ["Completed task", "Incomplete task"]);
});

test("entities decode once in prose and destinations while escapes, code and autolinks stay literal", () => {
  const doc = render("&copy; &amp; &#x41; &#38;copy; \\&amp; `<b>&amp;</b>`\n\n"
    + "[A &amp; B](https://example.com/?a=1&amp;b=2 \"A &amp; B\")\n\n"
    + "<https://example.com/?literal=&amp;>\n\n~~~html\n<b>&amp;</b>\n~~~");
  assert.equal(doc.querySelector("p")?.textContent, "© & A &copy; &amp; <b>&amp;</b>");
  const links = [...doc.querySelectorAll("a")];
  assert.equal(links[0].href, "https://example.com/?a=1&b=2");
  assert.equal(links[0].textContent, "A & B");
  assert.equal(links[0].title, "A & B");
  assert.equal(links[1].href, "https://example.com/?literal=&amp;");
  assert.equal(links[1].textContent, "https://example.com/?literal=&amp;");
  assert.equal(doc.querySelector("pre code")?.textContent, "<b>&amp;</b>\n");
  assert.equal(doc.querySelector("b"), null);
});

test("reference and formatted source links retain safe explicit navigation", () => {
  const doc = render("Source: [**Official** guide][guide].\n\n[guide]: https://example.com/a_(b)#part \"Guide\"");
  const link = doc.querySelector("a")!;
  assert.equal(link.querySelector("strong")?.textContent, "Official");
  assert.equal(link.href, "https://example.com/a_(b)#part");
  assert.equal(link.title, "Guide");
  assert.equal(link.target, "_blank");
  assert.equal(link.rel, "noopener noreferrer");
  assert.equal(doc.querySelectorAll("a").length, 1);
});

test("linked Markdown images use the enclosing link without nested anchors or image requests", () => {
  const doc = render("[**Open ![diagram](https://images.example/diagram.svg)**](https://example.com/details)");
  assert.equal(doc.querySelector("img"), null);
  assert.equal(doc.querySelectorAll("a").length, 1);
  assert.equal(doc.querySelector("a")?.href, "https://example.com/details");
  assert.equal(doc.querySelector("a strong")?.textContent, "Open diagram");
});

test("unsafe and entity-obfuscated destinations never produce navigable links", () => {
  for (const destination of ["javascript:alert(1)", "jav&#97;script:alert(1)", "java&Tab;script:alert(1)",
    "javascript&colon;alert(1)", "data:text/html,evil", "vbscript:evil", "file:///etc/passwd", "//evil.example/a",
    "https://user:password@example.com", "https://user&#58;password&#64;example.com", "https://example.com/&#10;path",
    "https://example.com/&#x7f;path", "/api/settings", "#composer"]) {
    const doc = render(`[**label**](${destination})`);
    assert.equal(doc.querySelector("a"), null, destination);
    assert.match(doc.body.textContent || "", /label/, destination);
  }
});

test("HTML and Markdown images cannot execute code or initiate resource loads", () => {
  const doc = render("![Diagram](https://outside.example/tracker.png) ![bad](data:image/svg+xml,evil)\n\n"
    + "<img src=https://outside.example/pixel onerror=alert(1)>\n\n"
    + "<script>alert('execute')</script>\n\n<style>@import 'https://outside.example/style';</style>\n\n"
    + "<iframe src=https://outside.example/frame></iframe>\n\n"
    + "<svg onload=alert(1)><image href=https://outside.example/svg /></svg>\n\n"
    + "<link rel=prefetch href=https://outside.example/prefetch>\n\n"
    + "<form action=https://outside.example/><input autofocus></form>\n\n"
    + "&lt;img src=https://outside.example/entity&gt;");
  assert.equal(doc.querySelector("img,script,style,iframe,svg,image,link,form,input,object,embed,video,audio,source"), null);
  assert.equal(doc.querySelector("[src],[srcset],[onerror],[onload],[autofocus]"), null);
  assert.equal(doc.querySelectorAll("a.markdown-image-link").length, 1);
  assert.equal(doc.querySelector("a.markdown-image-link")?.textContent, "Diagram");
  assert.equal(doc.querySelector<HTMLAnchorElement>("a.markdown-image-link")?.href, "https://outside.example/tracker.png");
  assert.ok([...doc.querySelectorAll("a")].every(link => link.protocol === "https:" && link.target === "_blank" && link.rel === "noopener noreferrer"));
  assert.match(doc.body.textContent || "", /<script>alert\('execute'\)<\/script>/);
  assert.match(doc.body.textContent || "", /<img src=https:\/\/outside.example\/entity/);
});

test("open, nested and tilde fences preserve code until a valid closing fence arrives", () => {
  const open = "Intro\n\n````markdown\n  **literal**\n```js\n  <script>&amp;</script>\n```\n";
  for (const text of [open, open + "``", open + "````\n\nAfter **code**"]) {
    const doc = render(text);
    assert.equal(doc.querySelectorAll("pre").length, 1);
    assert.match(doc.querySelector("pre code")?.textContent || "", /^  \*\*literal\*\*\n```js\n  <script>&amp;<\/script>\n```\n/);
    assert.equal(doc.querySelector("pre strong, pre script, pre a"), null);
  }
  assert.equal(render(open + "````\n\nAfter **code**").querySelector("p strong")?.textContent, "code");
  assert.equal(render("~~~python\n    print('safe')\n~~~").querySelector("pre code")?.textContent, "    print('safe')\n");
  assert.equal(render("    indented\n    code").querySelector("pre code")?.textContent, "indented\ncode\n");
});

test("incomplete streamed structures remain readable and become structured when complete", () => {
  const partial = render("## Re\n\n- first\n- seco\n\n| Name | State |\n| --");
  assert.equal(partial.querySelector("h2")?.textContent, "Re");
  assert.equal(partial.querySelectorAll("li").length, 2);
  assert.match(partial.body.textContent || "", /\| Name \| State \|/);
  assert.equal(partial.querySelector("table"), null);
  const complete = render("## Ready\n\n- first\n- second\n\n| Name | State |\n| --- | --- |\n| Chat | Ready |");
  assert.equal(complete.querySelector("h2")?.textContent, "Ready");
  assert.equal(complete.querySelector("tbody td")?.textContent, "Chat");
  assert.match(render("Source: [guide](https://example.com").body.textContent || "", /Source: \[guide\]\(https:\/\/example.com/);
  assert.equal(render("Source: [guide](https://example.com)").querySelector("a")?.textContent, "guide");
});

test("streamed and stored assistant text renders the same Markdown through Conversation", () => {
  const text = "## Result\n\n| Agent | Status |\n| --- | --- |\n| Explorer | **Done** |\n\n- Kept in chat";
  let streamed: Entry[] = [];
  for (let index = 0; index < text.length; index += 7) streamed = appendEvent(streamed, { event: "text_chunk", chunk: text.slice(index, index + 7) });
  streamed = appendEvent(streamed, { event: "text_done", text });
  const stored = fromHistory({ session_id: "markdown", retained_tasks: [], history: [
    { role: "assistant", content: text, name: "", tool_calls: [], tool_call_id: "" },
  ] });
  const content = (entries: Entry[]) => new JSDOM(renderToStaticMarkup(createElement(Conversation, {
    entries, sessionId: "markdown", demo: false, canSubmit: false, submit: () => assert.fail("Unexpected submit"),
  }))).window.document.querySelector(".markdown-content")!;
  assert.equal(content(streamed).innerHTML, content(stored).innerHTML);
  assert.equal(content(stored).querySelector("tbody strong")?.textContent, "Done");
});

test("focused child conversation renders the same Markdown before and after history refresh", () => {
  const text = "### Child findings\n\n- **Keep** images visible\n  - Support mobile\n\n| Check | Result |\n| --- | --- |\n| Layout | Done |";
  const detail: TaskDetail = {
    task: { id: "worker", name: "Explorer", status: "running", result: "", error: "", session_id: "root",
      prompt: "Inspect the layout", child_session_id: "child", parent_task_id: "", parent_session_id: "root",
      depth: 1, profile: "assistant", mode: "build", followups: 0, activation: 0, trigger: "delegation" },
    history: [], activity_cursor: 0,
  };
  const streamed = taskEntries(detail, [{ event: "task_activity", task_id: "worker", sequence: 1,
    timestamp: 1, activation: 0, payload: { type: "text_done", text } }]);
  const stored = taskEntries({ ...detail, activity_cursor: 1, history: [
    { role: "assistant", content: text, name: "", tool_calls: [], tool_call_id: "" },
  ] }, []);
  const content = (entries: Entry[]) => new JSDOM(renderToStaticMarkup(createElement(Conversation, {
    entries, sessionId: "child", demo: false, canSubmit: false, submit: () => assert.fail("Unexpected submit"),
  }))).window.document.querySelector(".markdown-content")!;
  assert.equal(content(streamed).innerHTML, content(stored).innerHTML);
  assert.equal(content(streamed).querySelector("h3")?.textContent, "Child findings");
  assert.equal(content(streamed).querySelector("li ul li")?.textContent, "Support mobile");
  assert.equal(content(stored).querySelector("tbody td")?.textContent, "Layout");
});

test("streaming retains the code region, focus and horizontal scroll as its content grows", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  try {
    const update = (text: string) => act(async () => root.render(createElement(MessageContent, { text })));
    await update("Text\n\n```python\nfirst_line");
    const pre = container.querySelector("pre")!;
    pre.focus();
    pre.scrollLeft = 80;
    for (const text of ["Text\n\n```python\nfirst_line\nsecond", "Text\n\n```python\nfirst_line\nsecond\n```\n\n**Finished**"]) {
      await update(text);
      assert.equal(container.querySelector("pre"), pre);
      assert.equal(dom.window.document.activeElement, pre);
      assert.equal(pre.scrollLeft, 80);
    }
    assert.equal(pre.querySelector("code")?.textContent, "first_line\nsecond\n");
    assert.equal(container.querySelector("p strong")?.textContent, "Finished");
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});
