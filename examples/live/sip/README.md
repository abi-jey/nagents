# Direct SIP

Run `python examples/live/sip/server.py`. Set `OPENAI_WEBHOOK_SECRET` and API
credentials discoverable by `OpenAIProvider()`. Forward a public HTTPS webhook
URL to `/webhooks/openai`; subscribe to `live.transport.incoming`.

Configure project access, TLS signaling and SRTP media according to the
[Live SIP guide](https://developers.openai.com/api/docs/guides/voice-sip?api=live).
For Telnyx, use its
[direct SIP setup](https://developers.telnyx.com/docs/voice/sip-trunking/gpt-live-configuration-guide).
Confirm current project-specific headers rather than assuming an older alpha
header is universal.

Use `--reject` on a separate call to exercise rejection. Set
`LIVE_TRANSFER_TARGET` to a controlled `sip:` or `tel:` destination for transfer.
HTTP success confirms a request, not that a human answered. Unknown outcomes and
claimed webhook deliveries remain in SQLite; reconcile them before retries.

The carrier owns primary playback. A sideband cannot itself play a verified
recording into the phone leg. Use a media bridge or carrier playback support when
that is required. Direct Live SIP does not create outbound calls.
