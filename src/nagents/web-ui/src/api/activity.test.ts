import assert from "node:assert/strict";
import test from "node:test";
import { pollActivity } from "./activity.js";
import type { ActivityReply } from "../types.js";

const reply = (
  cursor: number,
  changes: Partial<ActivityReply> = {},
): ActivityReply => ({
  session_id: "fixture session",
  cursor,
  events: [],
  active_run_id: "",
  pending_wakeups: [],
  truncated: false,
  ...changes,
});

test("polling starts at snapshot cursor, advances once, ignores repeated batch events and reports a gap once", async (t) => {
  const controller = new AbortController();
  const received: ActivityReply[] = [];
  const paths: string[] = [];
  const batch = reply(12, {
    events: [{ event: "text_chunk", run_id: "background", chunk: "once" }],
    active_run_id: "background",
    truncated: true,
  });
  const batches = [
    batch,
    batch,
    reply(13, {
      events: [
        { event: "run_finished", run_id: "background", status: "completed" },
      ],
    }),
  ];
  t.mock.method(
    globalThis,
    "fetch",
    async (path: string, options: RequestInit) => {
      paths.push(path);
      assert.equal(options.method, "GET");
      assert.equal(options.body, undefined);
      assert.equal(
        (options.headers as Record<string, string>)["X-Ngn-Token"],
        "fixture-token",
      );
      assert.equal(options.signal, controller.signal);
      return new Response(JSON.stringify(batches[paths.length - 1]));
    },
  );
  await pollActivity({
    token: "fixture-token",
    sessionId: "fixture session",
    cursor: 7,
    signal: controller.signal,
    interval: 0,
    receive: (data) => {
      received.push(data);
      if (received.length === 3) controller.abort();
    },
    failed: assert.fail,
  });
  assert.deepEqual(paths, [
    "/api/activity/fixture%20session/7",
    "/api/activity/fixture%20session/12",
    "/api/activity/fixture%20session/12",
  ]);
  assert.deepEqual(
    received.map((data) => data.events.length),
    [1, 0, 1],
  );
  assert.deepEqual(
    received.map((data) => data.truncated),
    [true, false, false],
  );
  assert.deepEqual(
    received.map((data) => data.active_run_id),
    ["background", "background", ""],
  );
});

test("an aborted slow poll cannot deliver a late response or overlap requests", async (t) => {
  const controller = new AbortController();
  let finish: (response: Response) => void = () =>
    assert.fail("request not started");
  let calls = 0;
  t.mock.method(globalThis, "fetch", () => {
    calls++;
    return new Promise<Response>((resolve) => {
      finish = resolve;
    });
  });
  const polling = pollActivity({
    token: "fixture-token",
    sessionId: "fixture session",
    cursor: 0,
    signal: controller.signal,
    interval: 0,
    receive: () => assert.fail("late response applied"),
    failed: assert.fail,
  });
  await new Promise((resolve) => setTimeout(resolve, 15));
  assert.equal(calls, 1);
  controller.abort();
  finish(new Response(JSON.stringify(reply(1))));
  await polling;
  assert.equal(calls, 1);
});

test("session changes isolate cursors and ignore a previous session's late response", async (t) => {
  const old = new AbortController();
  const current = new AbortController();
  let finishOld: (response: Response) => void = () => assert.fail();
  const paths: string[] = [];
  t.mock.method(globalThis, "fetch", (path: string) => {
    paths.push(path);
    if (path.includes("/old/"))
      return new Promise<Response>((resolve) => {
        finishOld = resolve;
      });
    return Promise.resolve(
      new Response(JSON.stringify(reply(41, { session_id: "new" }))),
    );
  });
  const first = pollActivity({
    token: "old-token",
    sessionId: "old",
    cursor: 5,
    signal: old.signal,
    receive: () => assert.fail("old session mixed in"),
    failed: assert.fail,
  });
  old.abort();
  await pollActivity({
    token: "new-token",
    sessionId: "new",
    cursor: 40,
    signal: current.signal,
    receive: (data) => {
      assert.equal(data.session_id, "new");
      current.abort();
    },
    failed: assert.fail,
  });
  finishOld(new Response(JSON.stringify(reply(6, { session_id: "old" }))));
  await first;
  assert.deepEqual(paths, ["/api/activity/old/5", "/api/activity/new/40"]);
});

test("invalid session, event session or regressing cursor is rejected without applying activity", async (t) => {
  for (const data of [
    reply(10, { session_id: "other" }),
    reply(8),
    reply(10, { events: [{ event: "notice", session_id: "other" }] }),
  ]) {
    const controller = new AbortController();
    const mock = t.mock.method(
      globalThis,
      "fetch",
      async () => new Response(JSON.stringify(data)),
    );
    let failure = "";
    await pollActivity({
      token: "fixture-token",
      sessionId: "fixture session",
      cursor: 9,
      signal: controller.signal,
      receive: () => assert.fail("invalid data applied"),
      failed: (message) => {
        failure = message;
        controller.abort();
      },
    });
    assert.match(failure, /Invalid background activity/);
    mock.mock.restore();
  }
});

test("read failures retry the same cursor without manufacturing run completion", async (t) => {
  const controller = new AbortController();
  const paths: string[] = [];
  let failures = 0;
  t.mock.method(globalThis, "fetch", async (path: string) => {
    paths.push(path);
    if (paths.length === 1) throw new Error("Offline fixture");
    return new Response(
      JSON.stringify(
        reply(3, {
          active_run_id: "still-running",
          pending_wakeups: [
            {
              wakeup_id: "w",
              task_id: "A",
              due_at: "2030-01-01T00:00:00Z",
              reason: "Review",
            },
          ],
        }),
      ),
    );
  });
  await pollActivity({
    token: "fixture-token",
    sessionId: "fixture session",
    cursor: 2,
    signal: controller.signal,
    interval: 0,
    receive: (data) => {
      assert.equal(data.active_run_id, "still-running");
      assert.equal(data.pending_wakeups.length, 1);
      controller.abort();
    },
    failed: () => {
      failures++;
    },
  });
  assert.equal(failures, 1);
  assert.deepEqual(paths, [
    "/api/activity/fixture%20session/2",
    "/api/activity/fixture%20session/2",
  ]);
});

test("an explicitly truncated cursor reset reports a gap and resumes read-only polling at the returned cursor", async (t) => {
  const controller = new AbortController();
  const paths: string[] = [];
  const received: ActivityReply[] = [];
  t.mock.method(globalThis, "fetch", async (path: string) => {
    paths.push(path);
    return new Response(
      JSON.stringify(
        paths.length === 1 ? reply(2, { truncated: true }) : reply(3),
      ),
    );
  });
  await pollActivity({
    token: "fixture-token",
    sessionId: "fixture session",
    cursor: 9,
    signal: controller.signal,
    interval: 0,
    receive: (data) => {
      received.push(data);
      if (received.length === 2) controller.abort();
    },
    failed: assert.fail,
  });
  assert.deepEqual(paths, [
    "/api/activity/fixture%20session/9",
    "/api/activity/fixture%20session/2",
  ]);
  assert.equal(received[0].truncated, true);
  assert.deepEqual(received[0].events, []);
});
