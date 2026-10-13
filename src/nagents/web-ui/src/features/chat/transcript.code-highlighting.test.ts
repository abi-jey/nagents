import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { CodeBlock, type CodeBlockProps } from "../../components/CodeBlock.js";
import { CODE_HIGHLIGHT_LIMIT } from "../../components/codeHighlighting.js";
import { MessageContent } from "./MessageContent.js";

function withCode(props: CodeBlockProps, check: (document: Document) => void) {
  const dom = new JSDOM(renderToStaticMarkup(createElement(CodeBlock, props)));
  try { check(dom.window.document); } finally { dom.window.close(); }
}

const examples = [
  { language: "yaml", aliases: ["yml"], text: "enabled: true\nitems:\n  - 'hello'\n", token: ".hljs-attr" },
  { language: "python", aliases: ["py", "python3"], text: "def greet(name):\n\treturn f'Hello {name}'  # preserved\n\n", token: ".hljs-keyword" },
  { language: "javascript", aliases: ["js", "jsx", "mjs", "cjs"], text: "const count = 42;\nconsole.log('ready');\n", token: ".hljs-keyword" },
  { language: "typescript", aliases: ["ts", "tsx", "mts", "cts"], text: "interface Result { count: number }\n", token: ".hljs-keyword" },
  { language: "json", aliases: ["jsonc"], text: '{ "id": 9007199254740993, "value": null, "path": "a\\\\b" }\n', token: ".hljs-attr" },
  { language: "bash", aliases: ["sh", "zsh", "shell"], text: 'if true; then\n  printf "%s" "$HOME"\nfi\n', token: ".hljs-keyword" },
  { language: "console", aliases: ["shellsession"], text: '$ echo "ready"\nready\n', token: ".hljs-meta" },
  { language: "html", aliases: ["xml", "xhtml", "svg"], text: '<section class="panel">safe &amp;</section>\n', token: ".hljs-name" },
  { language: "css", aliases: [], text: ".panel { color: #fff; width: 10px; }\n", token: ".hljs-attribute" },
  { language: "sql", aliases: [], text: "SELECT id FROM users WHERE id = 42;\n", token: ".hljs-keyword" },
  { language: "diff", aliases: ["patch"], text: "-old value\n+new value\n", token: ".hljs-addition" },
];

test("code blocks highlight selected local grammars and case-insensitive aliases without changing source", () => {
  for (const example of examples) {
    let expectedMarkup = "";
    for (const language of [example.language, ...example.aliases, ` ${example.language.toUpperCase()} `]) {
      withCode({ label: "Example", text: example.text, language }, document => {
        const code = document.querySelector("pre code")!;
        assert.equal(code.textContent, example.text, language);
        assert.ok(code.querySelector(example.token), `${language} should have a ${example.token} token`);
        assert.ok([...code.querySelectorAll("*")].every(element => element.tagName === "SPAN"));
        if (expectedMarkup) assert.equal(code.innerHTML, expectedMarkup, language);
        else expectedMarkup = code.innerHTML;
        const pre = document.querySelector("pre")!;
        assert.equal(pre.tabIndex, 0);
        assert.equal(pre.getAttribute("role"), "region");
        assert.equal(pre.getAttribute("aria-label"), "Example");
      });
    }
  }
});

test("missing, unsupported, and prototype-style language names remain literal plaintext", () => {
  const text = "\tconst value = '<script>&amp;</script>';  \n\n";
  for (const language of [undefined, "", "text", "plaintext", "unknown", "rust", "__proto__", "constructor", "toString", "python title=example", 'html\" onload=\"alert(1)']) {
    withCode({ label: "Literal", text, language }, document => {
      assert.equal(document.querySelector("code")?.textContent, text, String(language));
      assert.equal(document.querySelector("code *"), null, String(language));
      assert.equal(document.querySelector("script, [onload]"), null);
    });
  }
});

test("highlighted HTML, scripts, CSS and entity text cannot create active elements or resource attributes", () => {
  const text = '<script>alert("execute")</script>\n<img src="https://outside.example/pixel" onerror="alert(1)">\n'
    + '<style>@import "https://outside.example/style";</style>\n<iframe src="https://outside.example/frame"></iframe>\n'
    + '<svg onload="alert(1)"><image href="https://outside.example/image" /></svg>\n'
    + '<a href="javascript:alert(1)">link</a> &lt;script&gt; &amp; &#x41;\n';
  for (const language of ["html", "javascript", "css", "python"]) {
    withCode({ label: '<img src="https://outside.example/label">', text, language }, document => {
      assert.equal(document.querySelector("code")?.textContent, text);
      assert.equal(document.querySelector("img, script, style, iframe, svg, image, a, link, object, embed"), null);
      assert.equal(document.querySelector("[src], [href], [style], [onerror], [onload]"), null);
      assert.ok([...document.querySelectorAll("code *")].every(element =>
        element.tagName === "SPAN" && [...element.attributes].every(attribute => attribute.name === "class")));
    });
  }
});

test("large, many-line and token-dense snippets fall back to complete plaintext", () => {
  const sources = [
    "# " + "a".repeat(CODE_HIGHLIGHT_LIMIT),
    "x\n".repeat(1_001),
    "const x = 1; ".repeat(1_200),
  ];
  for (const text of sources) {
    withCode({ label: "Large source", text, language: "javascript" }, document => {
      assert.equal(document.querySelector("code")?.textContent, text);
      assert.equal(document.querySelector("code span"), null);
      assert.equal(document.querySelectorAll("pre").length, 1);
    });
  }
  withCode({ label: "At limit", language: "python", text: "#" + "x".repeat(CODE_HIGHLIGHT_LIMIT - 1) }, document => {
    assert.ok(document.querySelector(".hljs-comment"));
  });
});

test("Markdown passes fence language metadata to the reusable highlighter and keeps inline code literal", () => {
  const dom = new JSDOM(renderToStaticMarkup(createElement(MessageContent, {
    text: "`const inline = true`\n\n```Py title=example.py\ndef greet():\n    return '<b>&amp;</b>'\n```\n\n~~~yml\nenabled: true\n~~~",
  })));
  try {
    const document = dom.window.document;
    assert.equal(document.querySelector("p code span"), null);
    assert.equal(document.querySelector('[data-language="Py"] h3')?.textContent, "Code (Py)");
    assert.equal(document.querySelector('[data-language="Py"] code')?.textContent, "def greet():\n    return '<b>&amp;</b>'\n");
    assert.equal(document.querySelector('[data-language="Py"] .hljs-keyword')?.textContent, "def");
    assert.equal(document.querySelector('[data-language="yml"] .hljs-attr')?.textContent, "enabled:");
    assert.equal(document.querySelector("b"), null);
  } finally { dom.window.close(); }
});

test("streaming and fallback preserve exact whitespace, region identity, focus and scroll", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  try {
    const update = (text: string, language = "python") => act(async () => root.render(createElement(CodeBlock, { label: "Streaming code", text, language })));
    const start = "\r\n\tdef greet():\r\n\t\treturn 'héllo 🐍 &amp; <b>'  \r\n\r\n";
    await update(start);
    const section = container.querySelector("section")!;
    const pre = container.querySelector("pre")!;
    const code = pre.querySelector("code")!;
    assert.ok(code.querySelector("span"));
    assert.equal(code.textContent, start);
    pre.focus();
    pre.scrollLeft = 80;
    pre.scrollTop = 40;
    for (const [text, language] of [
      [start + "print('partial", "python"],
      [start + "print('partial')\n", "PY"],
      [start + "#".repeat(CODE_HIGHLIGHT_LIMIT), "python"],
      [start, "unrecognized"],
      [start, "python"],
    ]) {
      await update(text, language);
      assert.equal(container.querySelector("section"), section);
      assert.equal(container.querySelector("pre"), pre);
      assert.equal(pre.querySelector("code"), code);
      assert.equal(code.textContent, text);
      assert.equal(dom.window.document.activeElement, pre);
      assert.equal(pre.scrollLeft, 80);
      assert.equal(pre.scrollTop, 40);
      assert.equal(Boolean(code.querySelector("span")), language !== "unrecognized" && text.length <= CODE_HIGHLIGHT_LIMIT);
    }
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const key of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[key]) Object.defineProperty(globalThis, key, previous[key]);
      else Reflect.deleteProperty(globalThis, key);
    }
  }
});
