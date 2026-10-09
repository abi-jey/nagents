import assert from "node:assert/strict";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { JSDOM } from "jsdom";
import { McpDiscovery, type McpDiagnostics } from "../tools/McpDiagnostics.js";

const empty: McpDiagnostics = {
  status: "loaded", configured_servers: 0, connected_servers: 0, registered_tools: 0, advertised_tools: 0,
  effective_source: "none", error: "",
  sources: [{ path: "/user/.config/ngn/config.json", trusted: true, status: "read" },
    { path: "/workspace/.ngn/config.json", trusted: false, status: "missing" }],
  ignored_configs: ["/workspace/.vscode/mcp.json", "/workspace/opencode.json"],
};
const render = (value?: McpDiagnostics) => new JSDOM(renderToStaticMarkup(createElement(McpDiscovery, { value })));

test("zero MCP servers remain visible with checked paths, trust and ignored foreign configuration", () => {
  const dom = render(empty), document = dom.window.document;
  try {
    assert.match(document.body.textContent!, /0 MCP servers configured/);
    assert.match(document.body.textContent!, /0 connected.*0 tools registered.*0 available/);
    assert.equal(document.querySelector("details")?.open, true, "empty discovery makes the source explanation immediately accessible");
    assert.match(document.querySelector("details")?.textContent || "", /config\.jsonRead · trusted/);
    assert.match(document.querySelector("details")?.textContent || "", /config\.jsonMissing · not trusted/);
    assert.match(document.querySelector('[role="status"]')?.textContent || "", /\.vscode\/mcp\.json.*opencode\.json/);
    assert.match(document.body.textContent!, /does not import these files/);
    assert.match(document.body.textContent!, /after each model response/);
  } finally { dom.window.close(); }
});

test("failed reloads label retained counts and escape diagnostic paths without exposing unknown config values", () => {
  const dom = render({ ...empty, status: "retained", configured_servers: 2, connected_servers: 2, registered_tools: 8,
    advertised_tools: 4, error: "Resource reload failed: error=ValueError", effective_source: "/trusted/config.json",
    sources: [{ path: "/workspace/<private>.json", trusted: false, status: "ignored" }],
    ...{ command: "PRIVATE_COMMAND", env: { TOKEN: "PRIVATE_SECRET" } },
  });
  try {
    assert.match(dom.window.document.body.textContent!, /previous working configuration/);
    assert.match(dom.window.document.body.textContent!, /2 MCP servers configured.*2 connected.*8 tools registered.*4 available/);
    assert.equal(dom.window.document.querySelector("private"), null);
    assert.match(dom.window.document.body.textContent!, /<private>\.jsonIgnored · not trusted/);
    assert.doesNotMatch(dom.window.document.body.textContent!, /PRIVATE_COMMAND|PRIVATE_SECRET/);
  } finally { dom.window.close(); }
});

test("disabled and programmatic discovery does not claim configured servers were connected", () => {
  const dom = render({ ...empty, status: "disabled", configured_servers: 1, effective_source: "programmatic configuration", sources: [], ignored_configs: [] });
  try {
    assert.match(dom.window.document.body.textContent!, /1 MCP server configured.*0 connected/);
    assert.match(dom.window.document.body.textContent!, /Configured servers were not started/);
    assert.match(dom.window.document.body.textContent!, /does not read MCP declarations from files/);
    assert.equal(dom.window.document.querySelector('.tools-mcp-ignored'), null);
  } finally { dom.window.close(); }
  const legacy = render();
  try { assert.equal(legacy.window.document.body.textContent, ""); } finally { legacy.window.close(); }
});
