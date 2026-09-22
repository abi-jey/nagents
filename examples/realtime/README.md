# Realtime examples

These use the Realtime session/tool/turn protocol, separately from GPT-Live.

```bash
python examples/realtime/realtime_text.py
python examples/realtime/realtime_mic.py
python examples/realtime/realtime_voice.py --input speech.wav --output reply.wav
```

Set `OPENAI_API_KEY` in the environment or `.env`. Microphone/speaker examples
need the `voice` extra and PortAudio. Shared device and console helpers are in
`examples/_voice/`. GPT-Live's separate examples and guide index are in
[`examples/live/`](../live/README.md).
