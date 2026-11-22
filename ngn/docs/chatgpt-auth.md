# ChatGPT device authentication

> **Since 0.100.47** `ngn auth login|status|logout` runs the Python engine's own
> sign-in (`nagents.cli login`/`logout`) in the engine's Python; the Rust host
> and adapter paths described below were removed. Saved logins are the same ones
> the Python client uses. The `config.chatgpt.example.json` file mentioned below
> is no longer shipped; `ngn auth login chatgpt` sets up the connection.

**Official subscription release requires the matching published Python core.**
The adapter now requires `nagents>=0.100.2,<0.101`; that core must be published and
pass the fresh installed release gate first. Published `nagents==0.28.5` does
**not** contain the public `nagents.provider.device_auth` API. No compatibility
with that older core is implied for this adapter; the previous 0.100.1 API-key
release remains unchanged. The managed bundle supplies the matching core and
adapter. Source/small-native installations must prepare a matching runtime with
`OpenAIAuth`, `DeviceAuthorization`, `OpenAIAuthError`, and
`OpenAIProvider(credentials=auth.credentials, model=...)` themselves.

The tag-release build enforces this boundary in a fresh, non-editable environment:
the exact published core version and packaged adapter must expose the public auth
API and pass both synthetic native login/ownership tests before publication. Source
CI runs those tests explicitly as well; an unset optional binary selector cannot
silently skip them. These synthetic issuer tests do not validate a real account.

## Explicit connection and CLI

Copy `config.chatgpt.example.json` and choose a model your subscription can use.
The self-contained installed bundle supplies its own runtime when `runner` is
omitted. For a source/small-native installation, set `runner` to the trusted
environment's absolute Python executable and argv.
The host executes argv directly, never through a shell. On this explicit ChatGPT
route it does not consult Codex credentials or fall back to an API key. Implicit
discovery checks only for the presence of the Python ngn login file to avoid
switching accounts. If Python would use ChatGPT (or explicitly selects it),
use the native configuration below and sign in to the same intended account.
Rust never copies or refreshes credentials in the Python ngn store.

```sh
ngn --config /path/to/chatgpt-config.json auth status
# Stop every using host before either identity mutation:
ngn --config /path/to/chatgpt-config.json auth login
ngn --config /path/to/chatgpt-config.json auth logout
ngn --config /path/to/chatgpt-config.json run 'hello'
```

These commands are opt-in; merely starting ngn never initiates login. Enable
device authorization in ChatGPT security/workspace settings if required. Login
prints only the fixed `https://auth.openai.com/codex/device` address and a bounded
user code. Complete the authorization manually in your own browser. No browser
is opened and no approval is automated.

The integrated Linux development candidate was tested with a separately authorized
fresh login and `gpt-5.6-terra`: both a real PTY TUI and Chromium web conversation
used native read-only file tools and returned distinct expected fixture contents.
Both hosts joined their runners on shutdown, and the TUI restored terminal settings.
This confirms those bounded questions for that account, not general model
entitlement, refresh recovery, live steering delivery or full provider/UI parity.

`status` only inspects protected local storage: it does not refresh, resolve
credentials, or contact a network service. “Saved” means local credential
presence, not validity or model entitlement. Logout removes only the explicitly
selected native store; it is not a server-side token revocation. Cancellation
joins the owned auth subprocess, with bounded graceful/forced cleanup. Completed
identity changes are not rolled back; check status after interruption.

## Validation and storage

- `auth` defaults to `api_key` and is omitted from its original v1 wire shape.
- Only `kind: "openai"` may choose `auth: "chatgpt"`. Its `api_key_env` must be
  absent/empty; key values, callbacks, Codex homes and arbitrary auth paths are
  not valid configuration fields. Other configured API-key connections may coexist.
- Subscription `api` is empty/`auto`/`responses` only. `base_url` is empty or the
  exact official API default (`https://api.openai.com/v1`, optional trailing slash);
  it is validated but never passed as an OAuth endpoint override. Custom/public
  generation endpoints, query strings, mixed key settings and other APIs fail closed.
- Model IDs contain 1–128 ASCII letters/digits/dots/underscores/hyphens. This is a
  bounded ID, not an entitlement claim, catalog lookup, or hardcoded model allowlist.
- Rust generates the absolute `<data_dir>/auth/openai.json` path independently of
  workspace/runtime databases. Configuration and HTTP clients cannot set an
  `auth_store`. The adapter always constructs `OpenAIAuth(explicit_path)`; the
  legacy default store is never used.
- The public core owns plaintext credential storage with guarded POSIX directory
  handles, owned directories (700), regular singly-linked files (600), no
  symlinks, atomic replacement/fsync, cooperative process refresh locks, and
  revision/tombstone handling. This is not an OS keyring or sandbox. Use a local
  filesystem with reliable flock/atomic rename/fsync; unsafe/writable ancestors
  are rejected rather than silently repaired. Never delete lock/state metadata
  while another process might be using it.

## Identity ownership

Every native `Host` holds a **shared** flock on
`<data_dir>/.identity/host.lock`, including API-key hosts and hosts for other
workspaces. Subscription runtimes also retain a shared guard through their own
provider/auth cleanup, so an orphaned runner cannot lose identity protection
merely because its Rust parent exited.

Login/logout preflight in Rust and then acquire **exclusive** ownership in the
actual Python writer, before any login network request or credential mutation.
Rust intentionally releases the preflight guard before exec: the child must own
the real guard independently of its parent's lifetime. If a host starts in that
gap, the child fails closed as busy; if the child wins, the host cannot open.
Mutators also exclude each other, across processes/workspaces sharing `data_dir`.
Status is non-mutating and can run while hosts are active. Stop all using hosts
and let their runners finish before login/logout; this is enforced, not just an
operator promise. The stable empty metadata inode contains no tokens or PID and
is never unlinked. Locks are advisory between cooperative same-user applications,
not protection against arbitrary trusted shell/runtime code or hostile filesystem
replacement by the same user.

## Private protocols

Runtime protocol remains version 1. The adapter advertises `chatgpt_auth` **only**
if the new public core auth API is available. Rust must observe that capability
before sending either `provider.auth: "chatgpt"` or `run.auth_store`. An older v1
runtime still receives the unchanged API-key shape; subscription selection fails
with a redacted, actionable installation error instead of silently downgrading.

The separate subprocess mode is:

```text
python -m ngn_nagents auth login|status|logout --store /absolute/data/auth/openai.json
```

It accepts exactly those arguments and the fixed store suffix. Its private stdout
is NDJSON with a **2,048-byte frame limit**, protocol version 1, no arbitrary
messages, and only these shapes:

```json
{"auth_protocol":1,"message":{"type":"challenge","verification_url":"https://auth.openai.com/codex/device","user_code":"EXAMPLE-CODE"}}
{"auth_protocol":1,"message":{"type":"status","status":"signed_in"}}
{"auth_protocol":1,"message":{"type":"error","code":"missing_core"}}
```

Status is `signed_in`, `signed_out`, or `unusable`. Error codes are a fixed
allowlist; Rust supplies the human-readable explanation. Login allows at most one
challenge followed by one terminal result; status/logout allow no challenges.
Extra frames, secret/unknown fields, arbitrary URLs/codes, incomplete/oversized
frames, unknown versions and unsuccessful exits fail closed with redacted errors.
There is a 960-second login budget, a 15-second status/logout budget, and bounded
EOF/exit/cleanup waits. Stderr and upstream exception/server bodies are discarded.
Device-auth IDs, access/refresh/ID tokens and account routing credentials never
belong in this protocol, Rust config/events, browser bootstrap, or Node assets.

No HTTP login/logout routes or browser auth UI are provided. Browser identity
mutation/active-host gating, broader account/refresh coverage, macOS and non-local
filesystem validation remain separate unfinished gates. A live web steering attempt
ended before admission; no steering request or FIFO fallback was submitted, so
installed synthetic safe-point tests, not that attempt, establish steering behavior.
