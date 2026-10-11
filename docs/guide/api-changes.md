# API changes in 0.23

Version 0.23 removes superseded server implementations and compatibility APIs.
Update callers to the following interfaces before upgrading.

| Removed interface | Current interface |
| --- | --- |
| `python -m nagents.server`, `nagents[server]`, and the old `examples/k8s/deployment.yaml` | Install `nagents[web]` and run `ngn serve`; use `examples/k8s/ngn-web.yaml` for the private web deployment. |
| `POST /api/run` and its request-owned NDJSON stream | Submit a UUID-bearing request to `POST /api/messages` and subscribe to `/api/events`. Disconnecting a browser leaves accepted work running; `/api/cancel` explicitly stops a run. |
| `ngn.live.v1` | Negotiate `ngn.live.v2`, including playback interrupt controls. Audio still connects only to the ngn server. |
| Path-form Live delegation inspection | Use `/api/live/sessions/{session_id}/delegation-details?delegation_id=…`, encoding the complete opaque delegation ID. |
| Unscoped Live settings and their standalone API-key writes | Read `/api/live/settings?scope=global` or `scope=workspace`, and save scoped Voice preferences. Configure authentication in a named provider connection. |
| `api_key` / `clear_api_key` fields in general web settings | Send `{revision, values}`. Use Provider connections for authentication and routing. |
| `LiveConfig.client_handler` | Use `client_request_handler`, which receives `ClientDelegationRequest` from `nagents.live.delegation`. |
| Direct web bridge `handle` / `handle_native` calls and `LoginHandoff` | Attach an update sink and submit `ClientDelegationRequest` to `MainAgentBridge.handle_request`. Admission returns immediately; results arrive through the update sink and chat events. |
| `Compactor`, `DEFAULT_COMPACTOR`, `Tokens`, `Messages`, and Agent's `compactor` / `compact_on` / `compact_prompt` arguments | Use `compaction_strategy=ModelCompaction(...)` or a custom `CompactionStrategy`; use `None` to disable compaction. See [Compaction](compaction.md). |
| Compactor-only placeholder providers and sessions | Supply a real provider and session manager to an Agent. A `ModelCompaction` can use the requesting Agent's provider or an explicitly supplied summarizer provider. |
| `nagents.realtime.audio` and audio imports from `nagents.realtime` | Import shared audio primitives from `nagents.audio` or the top-level `nagents` package. |
| `nagents.stt.SUPPORTED_AUDIO_FORMATS` | Use `nagents.media.AUDIO_MIME_TYPES`. |
| Text-only `api="completions"` | Use a supported conversational contract: `chat_completions`, `responses`, or `messages`. Chat Completions remains supported. |
| Native `wake_up_in` tool | Call `schedule_wakeup`. Saved historical calls remain readable. |
| Implicit built-in agent names `build`, `agent`, and `reviewer` in configuration | Select `assistant`, with `read_only: true` when needed, or explicitly define a custom profile with the desired name. |
| `resource_settings(config)` | Use `read_resource_configuration(config)` and its `plugins` / `servers` fields. |
| `NAGENTS_SERVER_IMAGE`, `NETWORK`, `MEMORY`, `CPUS`, and `TIMEOUT` tool defaults | Use the corresponding `NAGENTS_DOCKER_*` variables for the standalone `docker_run` tool. |

Saved conversation history and database migrations remain supported. Existing
scoped Voice preferences are preserved; an older preference record is imported
only when no workspace preference record exists. Old credential records remain
untouched, and are never copied into Voice preferences or used to bypass named
provider authentication. Configure a provider connection before starting Voice
when upgrading an installation that used standalone Live credentials.

Custom channel implementations, provider-specific API contracts, custom tool
names, and explicit command aliases remain supported extension mechanisms.
