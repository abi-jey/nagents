import assert from "node:assert/strict";
import test from "node:test";
import { request, requestBinary } from "./client.js";
import { channelRequest } from "../features/channels/transport.js";

test("only expired workspace sessions navigate, once, without replaying writes", { timeout: 3000 }, async t => {
  const destinations: string[] = [];
  let notify!: () => void;
  const navigated = new Promise<void>(resolve => { notify = resolve; });
  Object.defineProperty(globalThis, "window", { configurable: true, value: { location: {
    pathname: "/dashboard", replace(path: string) { destinations.push(path); notify(); },
  } } });
  t.after(() => Reflect.deleteProperty(globalThis, "window"));
  let status = 401;
  let detail = "Provider sign-in failed";
  const fetcher = t.mock.method(globalThis, "fetch", async () => Response.json({ detail }, { status }));
  await assert.rejects(request("provider"), /Provider sign-in failed/);
  status = 403;
  detail = "Sign in to NGN.";
  await assert.rejects(request("sessions"), /Sign in to NGN/);
  assert.deepEqual(destinations, []);
  status = 401;
  const signal = new AbortController().signal;
  const errors: unknown[] = [];
  for (const op of [request("session-groups"), requestBinary("uploads", "test", new Blob(), {}, signal),
    channelRequest("test", signal, "refresh")]) void op.catch(e => errors.push(e));
  await navigated;
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(destinations, ["/dashboard"]);
  assert.deepEqual(errors, []);
  assert.equal(fetcher.mock.calls.length, 5);
});
