# A2A interoperability evidence

Local checks on 2026-10-20 used these pinned primary implementations:

| Component | Version / source |
| --- | --- |
| Protocol | [specification v1.0.1](https://github.com/a2aproject/A2A/tree/v1.0.1), negotiated wire version 1.0 |
| Product SDK | [a2a-python v1.2.3](https://github.com/a2aproject/a2a-python/tree/v1.2.3) |
| Official TCK | [1.0.0.alpha3 / 5daf0144b4a4adbdefb62180739b13c5bd7d8196](https://github.com/a2aproject/a2a-tck/tree/5daf0144b4a4adbdefb62180739b13c5bd7d8196); its vendored spec is v1.0.0 |
| Inspector | [8aa064639af106ff771d60428ef6d460f5454743](https://github.com/a2aproject/a2a-inspector/tree/8aa064639af106ff771d60428ef6d460f5454743), locked SDK 1.1.2 with protocol-1.0 support |
| Test IdP | Official Keycloak 26.8.1, separate disposable realm and clients |

These are interoperability results, not a certification or a claim that every
official TCK assertion passed.

## SDK and authorization tests

The standard SDK discovered the public card, sent authenticated work, consumed
status/artifact updates, retrieved/listed owned tasks, subscribed to an active
task and cancelled it. Closing a local stream left remote work running until
explicit cancellation. An identical retry did not execute the Agent twice.

The repository's optional A2A tests cover a native Agent and the ngn adapter,
owner isolation, changed-content message-ID rejection, input continuation,
approval/denial/expiry/cancel/replay and private storage. Real local Keycloak
checks separately exercise Client Credentials and user Authorization Code with
PKCE and access-token versus ID-token validation. A second real Keycloak flow
pauses a native Agent for downstream consent, validates an explicit user accept,
and resumes it with a distinct service token. The local protected resource
checks that token's signature, issuer, intended audience and scope. Tests also
check wrong-owner rejection, callback replay, private grant storage and absence
of tokens from public pending/completed tasks. Additional consent failure paths
run against an isolated HTTP IdP fixture.

The reproducible `examples.a2a.keycloak_fixture` helper starts that disposable
IdP on an assigned loopback port with generated private credentials. The
optional `NGN_A2A_KEYCLOAK_E2E=1` tests consume its private fixture path; see the
[example instructions](https://github.com/abi-jey/nagents/tree/main/examples/a2a).
The helper's import includes an explicit access-token subject mapper; a missing
`sub` is rejected by the product rather than replaced with an ID token.

The actual `ngn a2a serve` CLI was separately exercised with an isolated ngn
workspace, an actual Codex-backed `gpt-6.1-sol` model and the standard SDK client.
Its task completed after exactly one real `read_file` call/result. Server and
client provider/authentication were genuine; this was not the deterministic
protocol fixture. The private temporary server was stopped after the check.
No product authentication bypass is used.

## Official MUST TCK run

```sh
# In the pinned TCK's own isolated environment:
python run_tck.py --sut-host="$A2A_SUT_URL" --transport=jsonrpc --level must
```

Result: **61 passed, 11 failed, 163 skipped, 30 deselected**. The endpoint was a
native Nagents Agent with a deterministic provider fixture, not a real model.
The pinned TCK has no incoming Bearer configuration. A disposable loopback
proxy added a short-lived real Keycloak service token; product authentication
remained enabled. This tests protocol interoperability, not the authentication
boundary. The proxy also accepted the TCK's `/rpc/` form and forwarded it to the
advertised `/rpc`. It buffered SSE, so direct SDK subscription/cancellation
checks supplied the streaming evidence instead.

The failures need their fixture assumptions kept visible:

| Reported requirement | Finding |
| --- | --- |
| `CORE-EXECUTION-MODE-001/002`, `CORE-MULTI-001a/002a/003` | Independent tests reused the same `complete-task` message ID with different content. The retained-ID contract rejects this. `CORE-EXECUTION-MODE-001` passed alone against a fresh ledger. |
| `CORE-SEND-003` | The product returned HTTP 200 JSON-RPC 2.0 with matching ID and numeric `-32005` (`ContentTypeNotSupportedError`). The TCK requirement omitted `expected_error` and treated the expected error as an operation failure. |
| `DM-ART-001` | Subcases expected canned text, file, file-URL and data output. The fixture supplied generic text only. The text subcase passed when its provider returned the exact expected string. File/data generation was not supplied by this text-only adapter/fixture. |
| `DM-MSG-001` | The fixture expected a direct Message response. This service always creates an owned Task, including for simple answers. |

The pinned specification permits either a Task or a direct Message in
[Send Message and streaming](https://github.com/a2aproject/A2A/blob/v1.0.1/docs/specification.md#311-send-message),
and permits message-ID deduplication in
[idempotency](https://github.com/a2aproject/A2A/blob/v1.0.1/docs/specification.md#331-idempotency).
Returning an owned Task and refusing changed-content ID reuse are deliberate
service policies. The canned artifact cases do not establish support for file
generation; the service advertises and implements public text output only.
The official suite/assertions were not edited to turn these results green.

Raw JSON, HTML and runner logs were retained in the local test artifacts. The
initial proxy-path failure and fresh-ledger follow-ups are retained alongside
the final report rather than being described as a complete conformance pass.

## Inspector and actual model

Chrome DevTools drove the pinned Inspector's actual Connect and Send controls
against a disposable native service, using its normal backend and SDK. The
service used an actual `OpenAIProvider` model (`gpt-6.1-sol`), the ordinary
framework Agent loop and a read-only `machine_status` tool on that machine.
Inspector validated the Agent Card, selected JSON-RPC 1.0, and displayed valid
working/artifact/completed events with the returned OS/architecture/capacity.

One Inspector-only workaround was necessary. Its backend treats browser
`Origin`, `Referer` and `Cookie` as custom headers and forwards them upstream.
The first Connect failed because the service correctly rejected that unrelated
browser Origin. The disposable Inspector launcher added those three names to
its runtime `STANDARD_HEADERS` exclusion set before starting. Its checkout,
product code and authorization checks were unchanged. The same UI then
connected and completed the real-model request. This workaround is part of the
test provenance, not an unmodified-Inspector compatibility claim.

Inspector's debug events can expose custom authorization headers. Only a
short-lived disposable IdP token was supplied, in an isolated browser context;
no production/provider token was entered. Public card/chat JSON and a screenshot
were retained separately from private credential data. The browser console's
initial Connect error and Inspector-restart socket errors were preserved; no
additional error appeared during the successful task.
