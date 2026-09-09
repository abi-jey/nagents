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

### Your Own Connection

Model discovery uses the backend's **active provider**, not browser-supplied
credentials or a browser-selected endpoint. It never changes authentication or
billing mode. Keep an existing private deployment's ChatGPT/Codex login; enabling
catalog discovery does not require switching it to an OpenAI Platform API key.

For your own installation, choose your own connection deliberately:

- **OpenAI Platform API key:** set `OPENAI_API_KEY` securely in the backend
  environment using your own key, then run `ngn serve --provider openai --auth
  api-key --model gpt-4.1`. Platform API usage and billing are separate from a
  ChatGPT subscription. Never put key values into browser settings, URLs, or Git.
- **ChatGPT/Codex login:** run `ngn login --device-auth`, approve only the code you
  requested, and use `ngn login --status` to check the saved login. Then run
  `ngn serve --provider openai --auth chatgpt`, with the default endpoint and
  `api = "auto"`. Interactive `/login` remains available in the terminal client.
  Use your own eligible ChatGPT account; protected credential storage currently
  requires POSIX. See [device login](ngn.md#openai-device-login) for limitations.

The API-key and Codex catalogs are not interchangeable, and discovery never falls
back between them. In particular, a Codex OAuth access token is **not** an OpenAI
API key. Login/refresh remains on the backend; the browser receives only model
IDs and a non-secret source label. Administrator-managed containers use their
existing backend credential environment/store, not credentials supplied by a web
visitor. This does not add multi-user account isolation to a shared deployment.

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
- No remote binding, CORS wildcard, authentication/endpoint editor, voice,
  attachment upload, arbitrary file HTTP access, or direct shell endpoint is
  provided. Only built frontend assets are served. Unknown routes remain 404.

This is a **trusted local-user tool, not a sandbox or multi-user service**. Other
processes/users with access to your loopback interface may access it. Do not expose
the standalone server through a reverse proxy, public tunnel, port forward, or
shared remote desktop. For administrator-managed **password-free Tailscale access**,
see the [private deployment guide](ngn-web-deployment.md). That setup keeps ngn on
same-pod loopback behind nginx, uses HTTPS without Funnel or NodePort exposure,
and is not a general remote-access mode. Tailscale ACLs/grants and trusted cluster
networking are the access boundary: everyone who can reach the Service shares
the trusted-user workspace, sessions, and approvals, with no multi-user isolation.
ClusterIP is not public Internet exposure, but cluster clients can also reach it
and forge headers. Host/HTTPS Origin, fetch-metadata checks, and the per-process
CSRF token protect browser requests, not against malicious authorized network
clients that can obtain the bootstrap token. nginx strips incoming Authorization
headers before forwarding to ngn; there is no web login prompt.

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
| `GET settings` | Committed runtime values, startup defaults, profiles, revision, persistence and safe connection status; readable during a run |
| `GET models` | Explicit fresh discovery from the active provider; `{models: string[], source: string}`; read-only, including during a run |
| `POST settings` | `{revision, values}` validates and persists the complete allowlisted settings; idle only |
| `POST settings/reset` | `{revision}` restores startup defaults and deletes the saved override; idle only |
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

### Runtime Settings

Settings responses contain `values`, `defaults`, `profiles` (`name`, `mode`, `model`),
an opaque `revision`, `persisted`, `effective_mode` (`build` or `reviewer`), and
read-only `connection` (`provider`, `api`, `auth_status`). Both `values` and
`defaults` contain exactly these fields:

| Field | Accepted Values |
| --- | --- |
| `model` | Trimmed, nonblank provider model ID, at most 200 characters, without control characters |
| `agent` | An existing built-in or trusted configured profile name |
| `shell_timeout` | Finite number greater than 0 and at most 600 seconds |
| `max_output` | Integer, 1,024 through 1,048,576 **bytes of tool output**, not model tokens |
| `max_file_bytes` | Integer, 1,024 through 4,194,304 bytes |
| `max_tool_rounds` | Integer, 1 through 1,000 |
| `max_subagent_depth` | Integer, 0 through 8; root depth is 0 |

POST requires all seven values. Unknown fields, numeric strings, booleans used as
numbers, and nonfinite numbers are rejected with HTTP 422. Changing profile selects
its trusted instructions/mode, but the explicitly submitted model takes precedence
over that profile's model. Model IDs are free text: settings GET/save never query a model
catalog or test entitlement. A later provider request may reject an unavailable ID.
Permission ceilings and per-call approvals remain enforced. Existing sessions,
history, provider credentials, and tools are retained. New children inherit the
current model; retained children keep their prior state under the existing child
continuation contract.

Mutations reject HTTP 409 during a run, approval, or another mutation, or when the
submitted revision is stale. Reload before retrying; do not automatically overwrite
another tab's settings. Reads expose only the last committed snapshot, including
during a save. Error `detail` is a safe string, never raw request/provider data.

The versioned `ngn_web_settings` singleton row lives in the **existing workspace
session SQLite database**, resolved by the Harness under `data_dir`. It stores only
the seven approved values and revision, not credentials or the full configuration.
The override applies across sessions and web-server restarts for that resolved
workspace. One process must own the workspace; this is not multi-process settings
synchronization. ConfigMap/TOML, CLI, profile and initial authentication/model
resolution establish startup defaults **before** the saved override is applied.
Reset deletes the row, so later restarts use any newly changed trusted defaults.
The CLI/TUI do not load this web-only override.

A save joins its local SQLite transaction even if its HTTP request is cancelled;
on failure, live config, model, round limit and instructions are restored. If the
connection is lost, reload to determine whether the save committed before retrying.
Invalid JSON, unsupported versions, invalid values, or a removed saved profile block
web startup rather than silently restoring potentially more permissive defaults.
An administrator must stop ngn and repair or remove **only** the `ngn_web_settings`
row in the affected database; preserve session history and credential storage.
The API cannot edit provider routing, authentication, plugins, trust, paths, demo
mode, arbitrary files, or Python configuration.

### Model Discovery

`GET /api/models` is an explicit request, never a startup, bootstrap, or settings
read side effect. It accepts no query parameters and uses the same `X-Ngn-Token`,
Host, Origin, and fetch-metadata guards as other API reads. A successful response
contains only `models` (an array of IDs) and `source`: `codex` for an active
`CodexProvider`, otherwise the actual `ProviderType.value` such as
`openai_compatible`, `openrouter`, or `litellm`. The configured provider can still
be named `openai` while the active connection is Codex.

Offline demo and unsupported connections return HTTP **501** with a safe string
`detail`. Codex catalog discovery is currently explicitly unsupported pending a
verified upstream catalog contract; its existing login and generation still work.
Missing provider credentials, upstream errors, timeouts, and invalid
catalog data return HTTP **502**, not browser-authentication HTTP 401. A valid
empty upstream catalog returns HTTP 200 with `models: []`. Failures never supply
a fake or cached catalog, expose upstream exceptions, or change the provider.

Discovery does not change the selected model, settings values, persisted row,
revision, or browser draft, and it does not take over a running task or approval.
Selecting and saving a model remains a separate settings action. Manual model-ID
entry continues to work when discovery fails or is unsupported. IDs do not prove
capabilities or entitlement; a later generation request remains authoritative.
This endpoint and the library method are source-checkout features, not part of
the published `v0.5.0` web/API surface.

## Development Checks

The frontend is organized by responsibility within `src/nagents/web-ui/src/`:

- `app/App.tsx` composes the layout and features. `app/useClient.ts` coordinates
  connection, shared busy state, and explicit recovery.
- `features/chat/` contains conversation/composer rendering, the owning run-stream
  hook, and a tested transcript reducer/history adapter.
- `features/sessions/` owns session transport/state and session navigation.
- `features/approvals/` owns the exact pending decision and its accessible modal.
- `api/` contains HTTP transport with the per-process CSRF token and tested NDJSON parsing.
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
