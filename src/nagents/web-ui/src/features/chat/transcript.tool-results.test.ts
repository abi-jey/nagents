import assert from "node:assert/strict";
import test from "node:test";
import { act, createElement } from "react";
import { createRoot } from "react-dom/client";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { ExecutionRecord } from "./ExecutionRecord.js";
import { ExecutionValue } from "./ExecutionValue.js";
import { appendEvent } from "./transcript.js";
import type { Entry } from "./transcript.js";
import { presentToolResult, toolResultTokens, visibleToolText } from "./toolResultPresentation.js";

test("JSON formatting preserves integer IDs, exponent literals, escaped strings and duplicate fields", () => {
  const source = '{"id":9007199254740993,"id":1e999,"path":"a\\\\b","nested":[true,null,{"a":1.2300}]}';
  const result = presentToolResult(source);
  assert.equal(result.format, "JSON");
  assert.equal(result.structured, true);
  assert.equal(result.text, '{\n  "id": 9007199254740993,\n  "id": 1e999,\n  "path": "a\\\\b",\n  "nested": [\n    true,\n    null,\n    {\n      "a": 1.2300\n    }\n  ]\n}');
  assert.equal(toolResultTokens(result.text).map((token) => token.text).join(""), result.text);
});

test("JSON strings are decoded once, including wrapped objects and multiline text", () => {
  assert.deepEqual(presentToolResult('"line one\\nline two"'), { format: "JSON string", text: "line one\nline two", structured: false });
  const wrapped = presentToolResult(JSON.stringify('{"id":9007199254740993,"nested":[]}'));
  assert.equal(wrapped.format, "JSON string");
  assert.equal(wrapped.text, '{\n  "id": 9007199254740993,\n  "nested": []\n}');
  assert.equal(wrapped.structured, true);
  const dom = new JSDOM(renderToStaticMarkup(createElement(ExecutionValue, { label: "Result", text: '""', formatOutput: true })));
  assert.equal(dom.window.document.querySelector("pre")?.textContent, "(empty string)");
  dom.window.close();
});

test("JSON scalars stay distinguishable from missing content and plain output", () => {
  for (const text of ["null", "false", "0", "-3.400e-12"])
    assert.deepEqual(presentToolResult(text), { format: "JSON", text, structured: true });
  for (const text of ["", "normal output\nline 2\n", "{'python': True}", '{"partial":', "[output truncated]\n"])
    assert.deepEqual(presentToolResult(text), { format: "Text", text, structured: false });
});

test("JSON Lines retain every record and incomplete or mixed logs remain exact text", () => {
  const result = presentToolResult('{"step":1}\n\n{"step":2,"id":9007199254740993}\nfalse\n');
  assert.equal(result.format, "JSON Lines · 3 records");
  assert.equal(result.text, '{\n  "step": 1\n}\n\n{\n  "step": 2,\n  "id": 9007199254740993\n}\n\nfalse');
  for (const text of ['{"step":1}\n{"step":', '{"step":1}\nError: disconnected'])
    assert.equal(presentToolResult(text).text, text);
});

test("native shell output has readable sections and explicit reported error, timeout and truncation indicators", () => {
  const entry = appendEvent([], { event: "tool_result", id: "shell", name: "shell", result: {
    stdout: "first line\nsecond line", stderr: "Permission denied", exit_code: 7, timed_out: true, truncated: true,
  } })[0];
  const dom = new JSDOM(renderToStaticMarkup(createElement(ExecutionRecord, { entry, open: true })));
  try {
    const document = dom.window.document;
    assert.equal(document.querySelector('[aria-label="Result: standard output"]')?.textContent, "first line\nsecond line");
    assert.equal(document.querySelector('[aria-label="Result: standard error"]')?.textContent, "Permission denied");
    assert.deepEqual([...document.querySelectorAll(".tool-result-indicators > span")].map((item) => item.textContent), ["Exit 7", "Timed out", "Output truncated by tool"]);
    assert.equal(document.querySelectorAll(".tool-result-indicators [data-warning]").length, 3);
    assert.equal(document.querySelector(".execution-state")?.textContent, "Timed out");
    assert.equal(document.querySelectorAll("details").length, 1);
  } finally { dom.window.close(); }
});

test("shell formatting cannot discard unknown diagnostics, duplicate fields, invalid types, or reinterpret another tool", () => {
  const native = '{"output":"ran","exit_code":0,"timed_out":false,"truncated":false}';
  assert.deepEqual(presentToolResult(native, "shell").sections, [{ label: "Output", text: "ran" }]);
  assert.equal(presentToolResult(native, "custom_tool").sections, undefined);
  for (const source of [
    '{"output":"ran","provider_message":"Read this warning"}',
    '{"stdout":"first warning","stdout":"last warning"}',
    '{"output":"first","out\\u0070ut":"last"}',
    '{"output":"ran","exit_code":"error"}',
    '{"output":"ran","exit_code":9007199254740993}',
    '{"output":"ran","timed_out":"true"}',
    '{"output":"ran","truncated":{"reason":"quota"}}',
  ]) {
    const result = presentToolResult(source, "shell");
    assert.equal(result.sections, undefined);
    assert.equal(result.format, "JSON");
    assert.equal(result.text.replace(/\s/g, ""), source.replace(/\s/g, ""));
  }
});

test("complex and large results use bounded formatting and retain source errors and tail notices", () => {
  const deep = "[".repeat(40) + "0" + "]".repeat(40);
  const result = presentToolResult(deep);
  assert.equal(result.text, deep);
  assert.match(result.note!, /without reformatting/);
  const large = "start\n" + "x".repeat(150_000) + "\nError: important final line\n[output truncated]";
  const presentation = presentToolResult(large);
  assert.equal(presentation.text, large);
  assert.match(presentation.note!, /Large output/);
  const visible = visibleToolText(large);
  assert.ok(visible.text.startsWith("start\n"));
  assert.ok(visible.text.endsWith("Error: important final line\n[output truncated]"));
  assert.ok(visible.text.length < 33_000);
  assert.equal(visible.omitted, large.length - 32 * 1024);
  assert.deepEqual(toolResultTokens(large), [{ text: large, kind: "plain" }]);
});

test("tool result objects are formatted from their wire value and HTML remains inert text", () => {
  const entry = appendEvent([], { event: "tool_result", id: "data", name: "custom", result: {
    nested: { value: '<img src=x onerror="alert(1)"><script>alert(2)</script>' },
  }, error: "Transport warning: <b>partial result</b>" })[0];
  const dom = new JSDOM(renderToStaticMarkup(createElement(ExecutionRecord, { entry, open: true })));
  try {
    const document = dom.window.document;
    assert.equal(document.querySelector("img, script, b"), null);
    assert.match(document.querySelector('[data-panel="result"] pre')?.textContent || "", /"nested": \{\n/);
    assert.match(document.querySelector(".tool-error")?.textContent || "", /Transport warning: <b>partial result<\/b>/);
    assert.ok(document.querySelector(".tool-token-key"));
    assert.doesNotMatch(document.body.textContent!, /\[object Object\]/);
  } finally { dom.window.close(); }
});

test("matching shell JSON promotes a stream without replacing its focused reading surface", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  const source = JSON.stringify({ stdout: "evidence line\n".repeat(200), stderr: "", exit_code: 0 });
  let entry: Entry = { id: "shell-stream", kind: "tool", title: "shell", state: "Receiving output", text: source };
  const render = () => act(async () => root.render(createElement(ExecutionRecord, { entry, open: true })));
  try {
    await render();
    const stream = container.querySelector<HTMLElement>('[data-panel="stream"]')!;
    const reading = stream.querySelector("pre")!;
    reading.focus();
    reading.scrollTop = 45;
    entry = { ...entry, result: source, state: "Completed" };
    await render();
    assert.equal(container.querySelector('[data-panel="stream"]'), stream);
    assert.equal(container.querySelector('[data-panel="result"]'), null);
    assert.equal(stream.querySelector("pre"), reading);
    assert.equal(dom.window.document.activeElement, reading);
    assert.equal(reading.scrollTop, 45);
    assert.equal(reading.getAttribute("aria-label"), "Result");
    assert.match(reading.textContent!, /\{\n  "stdout":/);
    assert.equal(stream.querySelector(".tool-result-sections"), null);
    assert.match(stream.querySelector(".tool-result-indicators")!.textContent!, /Exit 0/);
    const raw = stream.querySelector<HTMLButtonElement>('[aria-label="Show raw result"]')!;
    await act(async () => raw.click());
    assert.equal(reading.textContent, source);
    await act(async () => raw.click());
    await render();
    assert.equal(stream.querySelector("pre"), reading, "Raw toggles and replay retain the original stream surface");
    assert.equal(reading.scrollTop, 45);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});

test("raw mode and copy preserve exact source while formatting, expanding and streaming update in place", async () => {
  const dom = new JSDOM("<div id='root'></div>");
  const previous = Object.getOwnPropertyDescriptors(globalThis);
  Object.assign(globalThis, { window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true });
  let copied = "";
  Object.defineProperty(globalThis, "navigator", { configurable: true, value: { clipboard: { writeText: async (value: string) => { copied = value; } } } });
  const container = dom.window.document.getElementById("root")!;
  const root = createRoot(container);
  let text = ' {"id":9007199254740993,"text":"hello\\nworld"}\n';
  const render = () => act(async () => root.render(createElement(ExecutionValue, { label: "Result", text, formatOutput: true })));
  try {
    await render();
    const pre = container.querySelector("pre")!;
    const raw = container.querySelector<HTMLButtonElement>('[aria-label="Show raw result"]')!;
    const copy = container.querySelector<HTMLButtonElement>('[aria-label="Copy result"]')!;
    assert.notEqual(pre.textContent, text);
    await act(async () => copy.click());
    assert.equal(copied, text);
    await act(async () => raw.click());
    assert.equal(raw.getAttribute("aria-pressed"), "true");
    assert.equal(pre.textContent, text);
    assert.equal(container.querySelector("pre"), pre);
    pre.focus();
    pre.scrollTop = 35;
    text += " ";
    await render();
    assert.equal(pre.textContent, text);
    assert.equal(container.querySelector("pre"), pre);
    assert.equal(dom.window.document.activeElement, pre);
    assert.equal(pre.scrollTop, 35);
    assert.equal(copy.textContent, "Copy");
    await act(async () => raw.click());
    text = "begin\n" + "x".repeat(40_000) + "\nfinal error";
    await render();
    assert.ok(pre.textContent!.endsWith("final error"));
    assert.ok(pre.textContent!.length < text.length);
    await act(async () => copy.click());
    assert.equal(copied, text, "Copy includes the middle omitted only from the display");
    const more = [...container.querySelectorAll<HTMLButtonElement>("button")].find((button) => button.textContent === "Show more")!;
    await act(async () => more.click());
    assert.equal(pre.textContent, text);
    assert.equal(container.querySelector("pre"), pre);
    assert.equal(container.querySelector(".tool-result-overflow"), null);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    for (const name of ["window", "document", "navigator", "IS_REACT_ACT_ENVIRONMENT"]) {
      if (previous[name]) Object.defineProperty(globalThis, name, previous[name]); else Reflect.deleteProperty(globalThis, name);
    }
  }
});
