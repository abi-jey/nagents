# Local Web Client

`ngn serve` serves a React client for the existing **ngn Harness**. It shares the
terminal client's workspace-scoped sessions, tools, provider configuration, and
per-call approvals. It does not use the older `nagents.server` application.

## Start

In your Python virtual environment, install the optional HTTP dependencies:

```bash
pip install 'nagents[web]'
ngn serve --workspace /path/to/project
```

Open **http://127.0.0.1:8765**. Use the exact host and port you started; `localhost`
and `127.0.0.1` are intentionally different origins. `--port 9876` selects another
port. The only accepted hosts are `127.0.0.1`, `::1`, and `localhost`.

Common Harness options work before or after `serve`, including `--provider`,
`--model`, `--agent`, `--config`, `--continue`, and `--resume`. API keys and saved
OAuth credentials stay on the backend. Configure credentials through the existing
CLI/environment, not the browser. Textual is not required. The existing `server`
extra also supplies the HTTP dependencies, but the applications are independent.

### Source Checkout

The React/TypeScript source and npm lockfile live in `web/`. Use Node 20.19 or newer:

```bash
pip install -e '.[web]'
npm --prefix web ci
npm --prefix web run build
ngn serve --demo --workspace /path/to/project
```

Vite builds into `src/nagents/web/static/`. These generated assets and
`web/node_modules/` are ignored by Git. Rebuild after editing frontend source.
There is no separate Vite origin/proxy required: test the production client through
`ngn serve`. Startup never downloads dependencies or builds assets. Missing assets
produce an actionable error; published distributions must include the build.

## Try It Offline

```bash
ngn serve --demo
```

Choose **Inspect workspace** for real streamed local tool events, then **Try an
approval** (or send `demo approval`). Review the sample diff and choose **Deny**,
or press Escape. **Allow Once** records only a demo decision; it does not write the
sample file. **Stop run** cancels an active run, including a pending approval.
Create a new session and select an older session to read/resume its conversation.

Demo responses are scripted. Demo retains the Harness restrictions: no model
requests, shell execution, workspace writes, or Python plugins. Session history is
still stored locally in the Harness data directory.

## Operation And Safety

- One local app instance owns one Harness on one async lifespan/event loop. Runs
  and session mutations conflict explicitly with HTTP 409; nothing is secretly
  queued. Another tab cannot submit into a different selected session by accident.
- Closing the stream, navigating away, cancelling, or shutting down cancels and
  joins the run and its child tools. Pending approvals fail closed. Completed
  actions are **not rolled back**.
- Approvals apply to an exact run, call ID, and fresh approval nonce. Only the
  active pending approval may be decided; stale, duplicate, or unknown decisions
  fail. An unanswered approval expires after five minutes and is denied.
- Partial streamed output remains visible after failure/cancellation. Prompts and
  decisions are never automatically retried. Reconnect explicitly to reload saved
  history; unfinished output may not have been persisted by the Harness. If another
  connection still owns a run, finish or cancel it before reconnecting.
- The client displays model and tool output as text, not HTML. Tool details are
  expandable. Enter sends, Shift+Enter inserts a newline; the approval dialog
  focuses Deny and traps focus using the native modal dialog.
- No remote binding, CORS wildcard, authentication/settings editor, voice,
  attachment upload, arbitrary file HTTP access, or direct shell endpoint is
  provided. Only built frontend assets are served. Unknown routes remain 404.

This is a **trusted local-user tool, not a sandbox or multi-user service**. Other
processes/users with access to your loopback interface may access it. Do not put it
behind a reverse proxy, public tunnel, port forward, or shared remote desktop.
Live approved shell/custom tools can access the host with your account's rights.
Closing the web server does not remove saved conversations or undo approved edits.

HTTP requests enforce the exact Host and Origin, reject cross-site fetches, and
require a random per-process token on API reads and mutations (except the
same-origin bootstrap). Mutations also require JSON and an Origin header; bodies
are limited to 64 KiB and prompts to 32,000 characters. The token is held in browser
memory, never a URL or local storage. Responses are non-cacheable, frame embedding
is blocked, and no third-party scripts/fonts are loaded.

## API Outline

All paths are under `/api`. API clients first GET `bootstrap`, then send its token
in `X-Ngn-Token`. POST requests also need `Origin: http://127.0.0.1:8765` (matching
the configured authority) and `Content-Type: application/json`.

| Method / Path | Purpose |
| --- | --- |
| `GET bootstrap` | Non-secret workspace/model/profile/demo info, token, active run ID |
| `GET sessions` | Selected session ID/history and this workspace's session list; idle only |
| `POST sessions/new` | `{}` creates/selects a session and returns the updated snapshot |
| `POST sessions/resume` | `{session_id}` checks workspace membership and returns its snapshot |
| `POST run` | `{session_id, prompt}` starts an owning NDJSON response |
| `POST cancel` | `{run_id}` cancels/joins exactly that active run |
| `POST approval` | `{run_id, approval_id, call_id, decision: "allow" or "deny"}` |

The run stream uses the CLI's normalized `schema_version: 1` event names and
adds `run_id` to every record. Web lifecycle records are `run_started`, `heartbeat`,
`approval`, `approval_closed`, and `run_finished` (`completed`, `failed`, or
`cancelled`). `approval.id` is the Harness call ID; `approval_id` is the fresh web
nonce. A core `done` is not the transport terminator; wait for `run_finished`.
Streams are not replayable/resumable. Read saved history to recover, rather than
automatically replaying a request with side effects.

## Development Checks

```bash
.venv/bin/pytest tests/test_web.py tests/test_cli.py
.venv/bin/ruff check src/nagents/web src/nagents/cli.py tests/test_web.py
.venv/bin/mypy src/nagents/web src/nagents/cli.py tests/test_web.py
npm --prefix web test
npm --prefix web run build
```
