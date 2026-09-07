# Local Web Client

`ngn serve` serves a React client for the existing **ngn Harness**. It shares the
terminal client's workspace-scoped sessions, tools, provider configuration, and
per-call approvals. It does not use the older `nagents.server` application.

## Source Availability

This guide describes the **current source checkout**, not the published `v0.5.0`
release. That release includes the existing ngn CLI, Harness, TUI, and headless
commands, but does **not** include `ngn serve` or the `web` extra. Install this
feature from the checkout using the commands below.

## Start From Source

From the repository root, with your Python virtual environment active and
Node 20.19 or newer installed:

```bash
python -m pip install -e '.[web]'
npm --prefix src/nagents/web-ui ci
npm --prefix src/nagents/web-ui run build
ngn serve --demo --workspace /path/to/project
```

For Poetry, use `poetry install -E web` instead of the editable pip command, run
the same npm commands, and launch with `poetry run ngn serve --demo --workspace
/path/to/project`. Omit `--demo` when you want configured live provider requests.

Open **http://127.0.0.1:8765**. Use the exact host and port you started; `localhost`
and `127.0.0.1` are intentionally different origins. `--port 9876` selects another
port. The only accepted hosts are `127.0.0.1`, `::1`, and `localhost`.

Common Harness options work before or after `serve`, including `--provider`,
`--model`, `--agent`, `--config`, `--continue`, and `--resume`. API keys and saved
OAuth credentials stay on the backend. Configure credentials through the existing
CLI/environment, not the browser. Textual is not required. The existing `server`
extra also supplies the HTTP dependencies, but the applications are independent.

### Frontend Build

The React/TypeScript source and npm lockfile live in `src/nagents/web-ui/`.
Vite builds into `src/nagents/web/static/`. These generated assets and
`src/nagents/web-ui/node_modules/` are ignored by Git. Rebuild after editing frontend source.
There is no separate Vite origin/proxy required: test the production client through
`ngn serve`. Startup never downloads dependencies or builds assets. Missing assets
produce an actionable error. A future published distribution with web support
must include this build; installing the published `v0.5.0` wheel is not a
replacement for these source-checkout steps.

## Try It Offline

```bash
ngn serve --demo
```

Choose **Inspect workspace** for real streamed local tool events, then **Review a
demo approval** (or send `demo approval`). Review the sample diff and choose **Deny**,
or press Escape. **Allow once** records only a demo decision; it does not write the
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
- The client displays model and tool output as escaped text, with fenced code
  shown in keyboard-scrollable code regions. Tool records retain separate inputs,
  streamed output, final result/error, call identity, and measured duration when
  provided. Parallel task records retain their IDs and parent/depth information.
- Missing results are never inferred to be successful. Resumed history labels
  results as recorded, since live duration/approval/status events are not persisted
  in the Harness message history.
- Enter sends and Shift+Enter inserts a newline. Approval dialogs initially focus
  the review heading, show exact inputs alongside the diff, and keep Deny conspicuous.
  Tab stays within the dialog, Escape denies, and closing restores focus. Finishing
  a stream does not steal focus or scroll a reader away from earlier messages.
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
`approval_closed` includes the backend's `decision` and `expired` flag so the live
transcript can retain the actual decision without guessing from tool output.
Streams are not replayable/resumable. Read saved history to recover, rather than
automatically replaying a request with side effects.

## Development Checks

The frontend is organized by responsibility within `src/nagents/web-ui/src/`:

- `app/App.tsx` composes the layout and features. `app/useClient.ts` coordinates
  connection, shared busy state, and explicit recovery.
- `features/chat/` contains conversation/composer rendering, the owning run-stream
  hook, and a tested transcript reducer/history adapter.
- `features/sessions/` owns session transport/state and session navigation.
- `features/approvals/` owns the exact pending decision and its accessible modal.
- `api/` contains authenticated HTTP transport and tested NDJSON parsing.
- `types.ts` defines shared backend contracts; `main.tsx` only mounts the app.

The Vite output contract is `../web/static` relative to `src/nagents/web-ui/`.
Python distributions include those generated assets, not frontend dependencies.

```bash
.venv/bin/pytest tests/test_web.py tests/test_cli.py
.venv/bin/ruff check src/nagents/web src/nagents/cli.py tests/test_web.py
.venv/bin/mypy src/nagents/web src/nagents/cli.py tests/test_web.py
npm --prefix src/nagents/web-ui test
npm --prefix src/nagents/web-ui run build
```
