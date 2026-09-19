import assert from "node:assert/strict";
import test from "node:test";
import { anchor, connect, endpoints, linkPath, nearestEndpoint } from "./connections.js";
import { newAgent, type Design } from "./types.js";

function design(): Design {
  return { version: 1, id: "team", entrypoint: "a", defaults: { provider: "primary", max_subagent_depth: 2 },
    providers: {}, secrets: {}, mcp_servers: {}, layout: {}, agents: { a: newAgent(), b: newAgent(), c: newAgent() } };
}

test("connections can attach to every side and arbitrary positions along a border", () => {
  const p = { x: 100, y: 200 };
  assert.deepEqual(anchor(p, "top", .25), { x: 145, y: 200, dx: 0, dy: -1 });
  assert.deepEqual(anchor(p, "bottom", .75), { x: 235, y: 268, dx: 0, dy: 1 });
  assert.deepEqual(anchor(p, "left"), { x: 100, y: 234, dx: -1, dy: 0 });
  assert.deepEqual(anchor(p, "right"), { x: 280, y: 234, dx: 1, dy: 0 });
  assert.deepEqual(nearestEndpoint("a", p, { x: 145, y: 198 }), { agent: "a", port: "top", offset: .25 });
  assert.deepEqual(nearestEndpoint("a", p, { x: 281, y: 251 }), { agent: "a", port: "right", offset: .75 });
  assert.deepEqual(nearestEndpoint("a", p, { x: 1000, y: 270 }), { agent: "a", port: "right", offset: 1 });
  assert.match(linkPath(anchor(p, "bottom"), anchor({ x: 100, y: 400 }, "top")), /^M 190 268 C 190 334, 190 334, 190 400$/);
});

test("source and target reconnection updates runtime links atomically and keeps documentation", () => {
  let value = connect(design(), { agent: "a", port: "bottom", offset: .25 }, { agent: "b", port: "top", offset: .8 });
  value.agents.a.invokes[0].description = "Research documents";
  let [from, to] = endpoints("a", value.agents.a.invokes[0]);
  assert.equal(from.port, "bottom"); assert.equal(to.port, "top");
  const original = value;
  value = connect(value, from, { agent: "c", port: "left", offset: .3 }, { source: "a", target: "b" });
  assert.equal(original.agents.a.invokes[0].agent, "b");
  assert.equal(value.agents.a.invokes[0].agent, "c");
  assert.equal(value.agents.a.invokes[0].description, "Research documents");
  [from, to] = endpoints("a", value.agents.a.invokes[0]);
  value = connect(value, { agent: "b", port: "right", offset: .6 }, to, { source: "a", target: "c" });
  assert.deepEqual(value.agents.a.invokes, []);
  assert.equal(value.agents.b.invokes[0].agent, "c");
  assert.equal(value.agents.b.invokes[0].description, "Research documents");
  assert.equal(value.agents.b.invokes[0].source_offset, .6);
});

test("duplicate or stale reconnection never removes the original link", () => {
  let value = connect(design(), { agent: "a", port: "right", offset: .5 }, { agent: "b", port: "left", offset: .5 });
  value = connect(value, { agent: "a", port: "right", offset: .5 }, { agent: "c", port: "left", offset: .5 });
  assert.equal(connect(value, { agent: "a", port: "top", offset: .5 }, { agent: "c", port: "bottom", offset: .5 }, { source: "a", target: "b" }), value);
  assert.equal(connect(value, { agent: "b", port: "top", offset: .5 }, { agent: "c", port: "bottom", offset: .5 }, { source: "b", target: "a" }), value);
  assert.equal(value.agents.a.invokes.length, 2);
});
