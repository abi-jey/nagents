import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { MessageContent } from "./MessageContent.js";
import { TextLinks } from "../../components/TextLinks.js";

function render(text: string, message = false) {
  return new JSDOM(renderToStaticMarkup(createElement(message ? MessageContent : TextLinks, { text }))).window.document;
}

test("source citations render descriptive clickable links in chat prose", () => {
  const doc = render("Source: [OpenAI’s Delegation and tools guide](https://developers.openai.com/api/docs/guides/live-delegation)", true);
  const link = doc.querySelector("a")!;
  assert.equal(link.textContent, "OpenAI’s Delegation and tools guide");
  assert.equal(link.href, "https://developers.openai.com/api/docs/guides/live-delegation");
  assert.equal(link.target, "_blank");
  assert.equal(link.rel, "noopener noreferrer");
  assert.equal(doc.body.textContent, "Source: OpenAI’s Delegation and tools guide");
});

test("links preserve punctuation, multiple citations, Unicode and balanced URL parentheses", () => {
  const doc = render("See [one](https://example.com/a_(b)#part), [次](<https://example.com/next>).\nStill text.");
  assert.deepEqual([...doc.querySelectorAll("a")].map(a => a.href), ["https://example.com/a_(b)#part", "https://example.com/next"]);
  assert.equal(doc.body.textContent, "See one, 次.\nStill text.");
});

test("partial streamed links remain readable until their closing delimiter arrives", () => {
  for (const text of ["Source: [guide]", "Source: [guide](https://example.com", "[", "[]()", "[a](https://example.com/a(b)"]) {
    const doc = render(text);
    assert.equal(doc.querySelector("a"), null);
    assert.equal(doc.body.textContent, text);
  }
  assert.equal(render("Source: [guide](https://example.com)").querySelectorAll("a").length, 1);
});

test("Markdown examples in code, escaped brackets and images stay literal", () => {
  const inline = "`[example](https://example.com)` \\[escaped](https://example.com) ![image](https://example.com/image.png)";
  assert.equal(render(inline).querySelector("a"), null);
  const code = render("```md\n[example](https://example.com)\n```\n[actual](https://example.com)", true);
  assert.equal(code.querySelectorAll("a").length, 1);
  assert.match(code.querySelector("pre")?.textContent || "", /\[example\]\(https/);
});

test("unsafe destinations and embedded HTML never become executable markup", () => {
  for (const url of ["javascript:alert(1)", "data:text/html,evil", "file:///etc/passwd", "//evil.example", "https://user:password@example.com", "https://example.com\t/path"]) {
    const text = `[link](${url})`;
    const doc = render(text);
    assert.equal(doc.querySelector("a"), null);
    assert.equal(doc.body.textContent, text);
  }
  const doc = render('[<img src=x onerror=alert(1)>](https://example.com) <script>alert(1)</script>');
  assert.equal(doc.querySelector("img,script"), null);
  assert.match(doc.querySelector("a")?.textContent || "", /<img/);
});
