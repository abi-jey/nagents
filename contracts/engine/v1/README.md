# Engine HTTP and event protocol (v1)

The source is `src/nagents/protocol` and `src/nagents/engine/api.py`.
`openapi.json` describes HTTP; `events.schema.json` describes frames on
`/api/v1/events` using the `ngn.events.v1` WebSocket subprotocol. A client
checks `/api/v1/info` before using the API. Both transports use the engine's
bearer token; it is not a browser credential.

Pass the last received `sequence` as `after` when reconnecting. IDs are never
reused by the same store. Transient deltas can leave gaps in replay; history
and resumed streams recover durable records, not dropped deltas. Decisions
must match the pending approval's ID, run ID and tool call ID; approval
records in history do not authorize anything.
When a stream is scoped to a root session it includes its child activations.
HTTP history stays scoped to the exact session named in the URL.

After changing the source, regenerate both exports and the Rust client using
Python 3.12+ and Rust 1.93.0's `rustfmt`:

```sh
python -m pip install -e . -r contracts/engine/v1/requirements.txt
PYTHONPATH=src python scripts/export_contract.py
python scripts/generate_rust_api.py
bash scripts/check_contract.sh
```

Update `fixtures.json` and the corresponding Rust tests when adding types.
The shared fixtures must round-trip in Python and Rust. Requests are strict;
clients can ignore additive response fields and preserve unknown event and
enumeration variants within a major version.
