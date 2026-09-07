# ngn configuration reference

This page describes the current source-checkout `ngn` harness, which can differ
from a published release or another coding assistant. The CLI is available in
`v0.5.0`; see [release availability and installation](ngn-installation.md) before
assuming every current field or behavior is present in that snapshot.

## Quick example

Configuration is **flat TOML**. Do not wrap it in `[ngn]`, `[provider]`, `[theme]`,
or `[dictation]`. Named agent profiles are the only nested configuration tables:

```toml
provider = "openai"
model = "gpt-4.1"
api = "auto"
auth = "api-key"
api_key_env = "OPENAI_API_KEY"
agent = "build"
theme = "terminal"
theme_background = "auto"
animations = true
submit_mode = "queue"
tab_action = "agent"
max_subagent_depth = 2

[profiles.audit]
mode = "reviewer"
instructions = "Prioritize regressions and missing tests. Do not edit files."
```

All top-level assignments must precede `[profiles.NAME]` sections: in TOML,
assignments following a table header belong to that table until the next header.
Unknown fields, wrong TOML types, invalid values, and literal `api_key` fields
are errors rather than silently ignored options.

The checkout includes an annotated, complete example at
`examples/harness/config.toml`. To try it
without provider calls, run from the repository root:

```bash
poetry run ngn --config examples/harness/config.toml --demo
```

## Precedence

The order is **lowest to highest priority**:

1. Built-in defaults.
2. `NGN_*` environment defaults.
3. Global/user TOML.
4. Trusted project TOML.
5. An explicitly selected TOML file.
6. Explicit CLI flags.

Environment variables are **not** the highest-priority override. For example,
`NGN_MODEL=environment-model` loses to `model = "file-model"` in global TOML;
`--model cli-model` overrides that top-level setting. A flag omitted from the
command line leaves the loaded value intact.

Files are layered, not exclusive: `--config` does not suppress the global file
or a project file allowed by `--trust-project`. Re-selecting the global file with
`--config` puts it at explicit-file priority, after the trusted project file.
An explicit file must exist; absent global and project files are fine.

Each loaded file must be valid TOML with valid field types. CLI flags are applied
after configuration loading and validation, so a CLI override is not a way to
repair a malformed or invalid lower-priority configuration. Likewise, invalid
environment conversions can fail before a file replaces the value.

Profile model selection happens at runtime after loading the top-level
configuration; see [profiles](#agent-profiles) before combining a profile-specific
`model` with `--model`.

## File locations and trust

| Item | Location or selection | Behavior |
| --- | --- | --- |
| Global/user TOML | `$XDG_CONFIG_HOME/ngn/config.toml`; defaults to `~/.config/ngn/config.toml` when `XDG_CONFIG_HOME` is unset or empty | Automatically trusted and loaded if present. |
| Project TOML | `<workspace>/.ngn/config.toml` | Ignored with a diagnostic unless `--trust-project` is supplied or that exact file is selected explicitly. |
| Explicit TOML | `--config /any/path/settings.toml` | Any filename/location is accepted. Selecting the file explicitly trusts it, including endpoint and plugin settings. |
| Workspace | `--workspace PATH` or `-C PATH`; defaults to the shell's current directory | Must already be a directory. Determines project configuration, file-tool boundaries, and session scope. |
| State root | `data_dir`; default `$XDG_DATA_HOME/ngn` or `~/.local/share/ngn` when `XDG_DATA_HOME` is unset or empty | Stores workspace-scoped session databases. |
| OpenAI login store | `$XDG_DATA_HOME/ngn/auth/openai.json` or `~/.local/share/ngn/auth/openai.json` | Separate credential store, not a TOML setting or a session database. Changing `data_dir` does not move it. Never include it in a config example or bug report. |

The workspace's `.ngn/config.toml` is the project file; ngn does not search every
ancestor directory for more configuration layers. The trust decision is made
from CLI arguments before project configuration is read. There is no
`trust_project = true` TOML escape hatch or `NGN_TRUST_PROJECT` shortcut.

```bash
ngn --workspace /path/to/project --trust-project
ngn --workspace /path/to/project --config /path/to/project/.ngn/config.toml
ngn --workspace /path/to/project --config /another/location/review.toml
```

The second command explicitly trusts the selected project file without needing
`--trust-project`. The third trusts only the explicitly selected file in addition
to global configuration; it does not implicitly trust a different workspace
project file.

Review trusted configuration **and its referenced Python code**. It can redirect
requests and select executable plugins. There is no safe-settings subset loaded
from an untrusted project file: the entire file is skipped.

### Relative paths

- `--workspace`, `--config`, and file paths passed with `--plugin` resolve from
  the shell's current working directory, not from each other. `-C` selects a
  workspace; it does not change how another CLI path is resolved.
- `data_dir` and `.py` plugin paths written in TOML resolve from the directory
  containing **that TOML file**. In `.ngn/config.toml`, `data_dir = "state"`
  means `.ngn/state`, and `plugins = ["../extension.py:setup"]` refers to
  `extension.py` at the workspace root.
- `NGN_DATA_DIR` is a path relative to the shell's working directory unless
  absolute. It is the full state root; ngn does not append another `/ngn` to it.
- Supported path values expand `~` and resolve to absolute paths. Use absolute
  XDG environment paths. General `$VARIABLE` or `${VARIABLE}` interpolation in
  TOML strings is not supported.
- An installed-module plugin such as `my_package.extension:setup` is imported
  from the selected Python environment, not resolved as a filesystem path.

## Complete top-level schema

Defaults below are built-ins, before environment, TOML, CLI, profile activation,
or authentication-route selection. String choices are case-sensitive.

### Provider and authentication

| Field | TOML type | Default | Accepted values and meaning |
| --- | --- | --- | --- |
| `provider` | string | `"openai"` | A provider name or alias from the table below. |
| `model` | string | `"gpt-4.1"` | Nonempty after trimming whitespace; a model ID supported by the endpoint/account, or an Azure deployment name. No model-catalog validation occurs at startup. |
| `api` | string | `"auto"` | `"auto"`, `"chat_completions"`, `"responses"`, `"messages"`, or `"completions"`. Selects the request protocol, not the authentication method. |
| `base_url` | string | `""` | Empty selects the provider default. Otherwise an HTTP(S) API-prefix URL with a hostname, no whitespace, user/password, query parameters, or fragment. Do not include a generation-route suffix. Required explicitly for LiteLLM and Azure. |
| `api_key_env` | string | `"OPENAI_API_KEY"` | Environment-variable name matching `[A-Za-z_][A-Za-z0-9_]*`, never a literal key. Changing provider does not automatically change this default. |
| `auth` | string | `"auto"` | `"auto"`, `"api-key"`, or `"chatgpt"`; see authentication behavior below. |
| `api_version` | string | `""` | Provider API version. Required for the versioned Azure route; not a general model/version selector. |

| Provider string | Aliases | Default endpoint/behavior |
| --- | --- | --- |
| `openai_compatible` | `openai` | `https://api.openai.com/v1`; API-key requests use the OpenAI-compatible protocol. |
| `openrouter` | None | `https://openrouter.ai/api/v1`; choose a model exposed by OpenRouter. |
| `anthropic` | None | `https://api.anthropic.com/v1`; native Messages protocol with `api = "auto"`. |
| `gemini_native` | `gemini`, `google` | `https://generativelanguage.googleapis.com/v1beta`; native Gemini protocol with `api = "auto"`. |
| `azure_openai_compatible` | `azure` | Explicit Azure resource endpoint and nonempty `api_version` required. |
| `azure_openai_compatible_v1` | None | Explicit Azure endpoint required; use the endpoint form expected by that route, rather than assuming the versioned Azure configuration is interchangeable. |
| `litellm` | None | Explicit `base_url` required. This is a connection to your gateway, not an embedded proxy or a LiteLLM SDK dependency. |

`api = "auto"` retains provider-specific routing: Chat Completions for
OpenAI-compatible, OpenRouter, LiteLLM, and Azure; Messages for Anthropic; native
Gemini for `gemini_native`. The four explicit API choices
refer to `/chat/completions`, `/responses`, `/messages`, and `/completions`
respectively under the configured API base, where that provider/endpoint supports
the route. Native Gemini routing is not one of those four OpenAI/Anthropic-style
paths. OpenAI-compatible, OpenRouter, and LiteLLM support selecting the four
explicit contracts, subject to server/model support. Anthropic accepts only
`auto` or `messages`; Azure accepts only `auto` or `chat_completions`; native
Gemini requires `auto`. OpenAI-compatible `messages` requires an explicit
compatible `base_url` rather than the default OpenAI endpoint.

`base_url` is a prefix such as `https://api.openai.com/v1`, not the full URL ending
in `/chat/completions`, `/responses`, `/messages`, or `/completions`. Those
generation suffixes are rejected during provider setup to avoid double-appending
a route. Provider setup also validates requirements beyond TOML parsing, such as
Azure's endpoint/version and whether a provider accepts the selected API.

**Legacy `completions` is text-only.** A server accepting `/completions` does not
make agent tools available. Coding, delegation, or another tool-dependent loop
requires an endpoint/model/protocol combination that actually supports tool
calling; a route selector cannot add that capability. The library's legacy route
accepts one text-only user prompt, not tools, multimodal input, or a flattened
system/conversation history. Consequently the ngn coding harness rejects
`api = "completions"` during setup, including in demo mode, because it requires
instructions and tools. Use the library's `Provider` directly for the restricted
text-only route. This caveat applies to LiteLLM and other compatible services too.

`auth = "auto"` prefers a saved ChatGPT login for the default OpenAI provider
endpoint only when `api = "auto"`; otherwise it uses `api_key_env`. `"api-key"` forces API-key selection.
`"chatgpt"` requires an ngn login and uses the dedicated Codex Responses route,
not a general-purpose OpenAI API credential. It requires `provider = "openai"`
or `"openai_compatible"` without `base_url` and with `api = "auto"`. Explicitly
combining `auth = "chatgpt"` with an `api` override is rejected, even for
`api = "responses"`. When explicitly selecting an API-key protocol, set
`auth = "api-key"` alongside `api` instead of relying on automatic login
selection. Saved OAuth credentials are never sent to a custom endpoint,
including LiteLLM. Do not use `api` to attempt to
redirect a ChatGPT login to a gateway or to the ordinary paid API.

The key's **value** is read from the named environment variable when a live
request needs it. `NGN_API_KEY_ENV=TEAM_OPENAI_KEY` changes the reference; it does
not set the key itself. ngn does not automatically load `.env` files or borrow
another coding assistant's credentials.

### Agent and tool limits

| Field | TOML type | Default | Accepted values and meaning |
| --- | --- | --- | --- |
| `agent` | string | `"build"` | `"build"`, `"agent"`, `"reviewer"`, or a configured profile name. |
| `shell_timeout` | integer or float | `60.0` | Seconds, strictly greater than `0` and at most `600`. Booleans are not numbers here. |
| `max_output` | integer | `32768` | `1024` through `1048576`, inclusive; bounds tool output in bytes. |
| `max_file_bytes` | integer | `262144` | `1024` through `4194304`, inclusive; file-size limit in bytes for guarded file operations. |
| `max_tool_rounds` | integer | `30` | `1` through `1000`, inclusive; maximum model/tool rounds in the agent loop. This is not the subagent count. |
| `max_subagent_depth` | integer | `2` | `0` through `8`, inclusive. Root depth is `0`, child depth `1`, grandchild depth `2`. `0` disables delegation; the default allows children and grandchildren, not great-grandchildren. |
| `demo` | boolean | `false` | Offline scripted provider, no workspace mutations or shell execution, and no plugin imports. Local demo conversations are still saved. |

Integer fields reject floats and booleans even if numerically equivalent.
Subagent concurrency, per-run job counts, timeouts, and result-size budgets are
additional safeguards, not extra TOML fields. See
[subagents](ngn.md#native-background-subagents).

### Terminal behavior

| Field | TOML type | Default | Accepted values and meaning |
| --- | --- | --- | --- |
| `theme` | string | `"terminal"` | `"terminal"`, `"graphite"`, `"ocean"`, or `"ember"`. |
| `theme_background` | string | `"auto"` | `"auto"`, `"terminal"`, or `"theme"`. Automatic preset behavior, terminal-native background, or the theme's background. Separate from palette selection. |
| `animations` | boolean | `true` | Enable motion; `false` disables the busy animation, animated scrolling, and cursor blinking. `TEXTUAL_ANIMATIONS=none` also disables them; brief button feedback remains. |
| `submit_mode` | string | `"queue"` | `"queue"` or `"interrupt"`: queue new prompts during work, or cancel/await current work before sending the new prompt. |
| `tab_action` | string | `"agent"` | `"agent"`, `"complete"`, or `"focus"`: cycle profiles, complete slash commands, or navigate widgets. |

These are ngn settings, not arbitrary Textual theme names or CSS tables. Preset
colors can evolve; there is no user-defined palette table in this schema.
With `theme_background = "auto"`, `terminal` uses native terminal surfaces and
the other presets use painted surfaces. `"terminal"` retains native surfaces for
any preset, with preset-specific ANSI accents. `"theme"` paints the background;
the `terminal` preset then uses a dark graphite-based surface fallback.

### Dictation

| Field | TOML type | Default | Accepted values and meaning |
| --- | --- | --- | --- |
| `dictation_enabled` | boolean | `false` | Explicit opt-in to microphone transcription. Installing the extra alone does not enable it. |
| `dictation_model` | string | `"gpt-4o-mini-transcribe"` | Nonempty after trimming whitespace; transcription model ID, separate from the conversation model. |
| `dictation_base_url` | string | `"https://api.openai.com/v1"` | Nonempty HTTP(S) URL with a hostname; no whitespace/control characters, embedded credentials, query parameters, or fragments. Separate from the conversation endpoint. |
| `dictation_api_key_env` | string | `"OPENAI_API_KEY"` | Environment-variable name matching `[A-Za-z_][A-Za-z0-9_]*` for the transcription API key, never a literal secret. |
| `dictation_language` | string | `""` | Empty for auto-detection, or exactly two lowercase ASCII letters matching `[a-z]{2}`, for example `"en"`. The service must support the code. |
| `dictation_max_seconds` | integer | `120` | `1` through `300`, inclusive; maximum recording duration in seconds. |

Dictation returns an **editable preview**, not an automatically submitted chat
message. It is not the realtime speech-to-speech library workflow. The
transcription endpoint requires its own paid API access; ChatGPT device login
and subscription benefits do not authorize this call. See
[dictation usage and privacy](ngn.md#dictation).

### Storage and extensions

| Field | TOML type | Default | Accepted values and meaning |
| --- | --- | --- | --- |
| `data_dir` | string path | `$XDG_DATA_HOME/ngn` or `~/.local/share/ngn` | Full session-state root; converted to an absolute `Path`. TOML-relative paths use the file's directory. |
| `plugins` | array of strings | `[]` | Ordered `"path.py:setup"` or `"installed.module:setup"` references. Converted to a tuple internally. Each higher-priority TOML list replaces the previous list. |
| `profiles` | table of profile tables | No custom profiles | Entries under `[profiles.NAME]`, merged by name with whole-profile replacement. Built-in profiles remain available. |

`workspace` (`Path`), `trust_project` (`bool`, default `False`), and `diagnostics`
(`tuple[str, ...]`, initially empty) also exist on the Python `HarnessConfig`
object. They are runtime/loader inputs or outputs, **not accepted TOML fields**.
Select workspace and trust through the CLI; diagnostics are generated by loading.

## Environment defaults

Every configurable top-level **scalar** field in the tables above has an
`NGN_<UPPERCASE_FIELD_NAME>` environment default. This includes
`NGN_API`, `NGN_MAX_SUBAGENT_DEPTH`, `NGN_THEME_BACKGROUND`, and all six
`NGN_DICTATION_*` fields as well as provider, model, limits, and state storage.

```bash
NGN_THEME=ocean NGN_ANIMATIONS=false ngn --demo
NGN_SUBMIT_MODE=interrupt NGN_MAX_SUBAGENT_DEPTH=1 ngn
NGN_API_KEY_ENV=TEAM_OPENAI_KEY ngn --auth api-key
```

TOML can override every one of these defaults. Booleans accept case-insensitive
`true`/`false` or `1`/`0` in the environment. Integer fields are converted with
integer parsing; `shell_timeout` uses floating-point parsing. Normal value/range
validation still applies.

There is no `NGN_PLUGINS` array or `NGN_PROFILES` table parser, no per-profile
environment-variable convention, and no `NGN_CONFIG`, `NGN_WORKSPACE`, or
`NGN_TRUST_PROJECT` configuration mechanism. Use TOML, `--config`, `--workspace`,
`--trust-project`, and repeatable `--plugin` flags as appropriate. Unknown
`NGN_*` environment names are not a substitute for supported fields.

## Agent profiles

Built-in names are reserved and cannot be overwritten in TOML:

| Profile | Mode | Purpose |
| --- | --- | --- |
| `build` | `build` | Default interactive profile. Guarded changes and shell require approval. |
| `agent` | `build` | General-purpose built-in profile; the default target of `delegate`. Subject to the parent's permission ceiling when used as a child. |
| `reviewer` | `reviewer` | Inspection only; edits, writes, shell execution, and custom tools are denied. |

Custom profile names match `[A-Za-z0-9_-]+`. Each profile accepts exactly these
string fields:

| Profile field | Default | Meaning |
| --- | --- | --- |
| `mode` | `"build"` | `"build"` or `"reviewer"`; `"agent"` is a profile name, not a mode. |
| `instructions` | `""` | Trusted profile instructions, appended to the harness context. They do not grant extra permissions. |
| `model` | `""` | Optional model override on profile activation. Empty keeps the current/top-level model. |

```toml
agent = "audit"

[profiles.audit]
mode = "reviewer"
instructions = "Report bugs with file references and explain missing tests."

[profiles.implementation]
mode = "build"
instructions = "Make the smallest correct change and verify it."
```

Activate with `--agent audit` or `/agent audit`; Tab normally cycles profiles.
A nonempty profile `model` is applied when the harness activates that profile,
after top-level `model` loading, so it can also supersede the value supplied by
`--model`. Leave a profile's `model` empty if you want top-level model selection
to govern it. In-process `/model` changes are runtime state, not edits to TOML.

### Merge and replacement rules

Profiles merge **by name**, not recursively by field. Different names from
different files remain available, but a later `[profiles.audit]` constructs a
new profile and replaces the earlier `audit` completely. Omitted fields in that
replacement return to the profile defaults.

For example, if global TOML defines `audit` with `mode = "reviewer"` and a model,
then a trusted project file containing only:

```toml
[profiles.audit]
instructions = "Focus on tests."
```

replaces it with a **build-mode** profile and an empty model override. It does
not inherit `mode = "reviewer"`. Repeat the intended mode when redefining a
read-only profile. An empty `[profiles]` table does not clear previously defined
profiles, and no profile deletion syntax is provided.

Delegation defaults to `agent`, rather than silently forcing every child into
`reviewer`. Child authority can never exceed the parent's effective permissions:
a reviewer parent cannot create a build-capable child by selecting `agent`,
`build`, or a custom profile. Depth limits count the root as zero. Profile
instructions cannot bypass approvals, depth limits, or the permission ceiling.

## Plugins

```toml
plugins = ["./extension.py:setup", "team_extensions.review:setup"]
```

The setup name must be a Python identifier. Installed module names must be
dot-separated identifiers. References are checked when loading configuration;
Python import and setup happen when the harness initializes, not during TOML
parsing. A setup function may be synchronous or asynchronous and may return an
`AgentPlugin` or `None`.

TOML plugin lists **replace** the previous list, including `plugins = []` to clear
lower-priority entries. Repeated `--plugin` flags **append** to the final TOML
list in CLI order. There is no automatic deduplication of plugin references;
listing one twice can invoke setup twice and can fail on duplicate registrations.

```bash
ngn --config /path/to/settings.toml --plugin /path/to/extension.py:setup
ngn --plugin examples/harness/commands.py:setup
```

Plugins are fully trusted Python code, not a sandbox or just prompt text. They
can access the network and filesystem during import/setup. `ngn doctor` normally
initializes them too, so it is **not** a safe way to inspect unknown Python code.
`ngn doctor --demo` skips plugin imports but still validates configuration and
initializes local demo state. Subagents do not re-import parent plugin files.
See [Python extensions](ngn.md#python-behavior-extensions) for the actual hook and
command interfaces.

## Provider recipes

These snippets are standalone, secret-free TOML files. Use a model ID your
account/gateway exposes. Put API-key values in your shell or secret manager, not
in these files, URLs, or command arguments.

### OpenAI API key

```toml
provider = "openai"
model = "gpt-4.1"
api = "auto"
auth = "api-key"
api_key_env = "OPENAI_API_KEY"
```

For eligible ChatGPT/Codex access instead, leave `base_url` unset, use
`auth = "auto"` or `"chatgpt"`, and follow [device login](ngn.md#openai-device-login).
That route can select a different account-supported default model.

### OpenRouter

```toml
provider = "openrouter"
model = "openai/gpt-4.1-mini"
api = "chat_completions"
auth = "api-key"
api_key_env = "OPENROUTER_API_KEY"
```

The default endpoint is `https://openrouter.ai/api/v1`; no `base_url` override is
needed. Set the referenced key in your launcher environment. This model was used
for a live ngn read-tool/response smoke test; choose another model if preferred,
but check that it supports tools. An explicit environment-file launch through uv
is shown in the [installation guide](ngn-installation.md#explicit-environment-files).

### Anthropic

```toml
provider = "anthropic"
model = "YOUR_ANTHROPIC_MODEL_ID"
api = "auto"
auth = "api-key"
api_key_env = "ANTHROPIC_API_KEY"
```

### Native Gemini

```toml
provider = "gemini"
model = "YOUR_GEMINI_MODEL_ID"
api = "auto"
auth = "api-key"
api_key_env = "GEMINI_API_KEY"
```

### LiteLLM gateway

```toml
provider = "litellm"
base_url = "http://127.0.0.1:4000/v1"
model = "YOUR_GATEWAY_MODEL_ALIAS"
api = "chat_completions"
auth = "api-key"
api_key_env = "LITELLM_API_KEY"
```

Configure the base URL and model alias for **your running server**; the example
does not start a gateway or guarantee a route exists. For a server exposing
Responses or Messages, choose `responses` or `messages` and the corresponding
API base. The library also exposes legacy Completions, but ngn rejects that
text-only route during harness setup. Gateway authentication uses the
referenced `LITELLM_API_KEY`, never an ngn ChatGPT OAuth token. Legacy Completions
remains text-only, not a promise of coding-tool support.

LiteLLM 1.100.0 has a legacy streaming usage-frame serialization issue. On that
one provider/route combination, ngn does not request the optional extra usage
frame; it still parses usage if the gateway supplies it. Error envelopes and
incomplete streams remain errors, not successful responses.

### Another OpenAI-compatible endpoint

```toml
provider = "openai_compatible"
base_url = "http://127.0.0.1:8000/v1"
model = "YOUR_SERVER_MODEL_ID"
auth = "api-key"
api_key_env = "LOCAL_MODEL_API_KEY"
```

Use the endpoint's required key in that environment variable. The harness still
expects a nonempty referenced key for a live API-key request; do not assume it
detects anonymous local servers automatically.

## Inspect and diagnose

`ngn doctor --demo` provides local configuration diagnostics without model calls
or Python plugin imports. `/context` shows effective runtime context, and
`/plugins` shows configured/loaded extensions. A real `ngn doctor` loads trusted
plugins, which can perform arbitrary I/O. These commands can create local state;
they are not side-effect-free TOML parsers.

If a setting is unexpected, check the global file, the workspace selected with
`-C`, whether project trust was granted, the explicit file, and CLI flags in that
order. Also check profile-specific models and runtime `/model` or `/agent`
changes. Do not attach credential files, session databases, `.env` contents, or
literal API keys to a diagnostic report.
