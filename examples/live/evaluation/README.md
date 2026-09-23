# Evaluation

- **Crawl:** generate and inspect a caller WAV once, then run `replay.py caller.wav`.
- **Walk:** use an approved human recording with the same task. Replay variants
  with noise/echo/packet loss while retaining the original expected outcome.
- **Run:** `python examples/live/evaluation/simulation.py --duration 90` connects
  an independent caller to an assistant with a real in-memory reservation tool.
  The caller's private goal is not supplied to the evaluated assistant.

Both scripts use native Nagents Live support. Audio is continuously paced; no
manual response turns or fictional audio-done events are generated. Replay saves
WAV output and event JSONL. Simulation saves task state, separate voice usage and
raw mono PCM16/24kHz relay recordings. Each relay starts when its consumer connects;
retain those startup differences when comparing timing, rather than assuming the
two raw files have identical time origins.

The simulation's deterministic grade checks the actual reservation record, not a
spoken claim. Listen to the output and inspect timing/interruptions separately.
Repeat scenarios and report sample counts, distributions and infrastructure
failures. Both simulated caller and assistant incur API costs. For a larger
evaluation, use the [guide's Crawl/Walk/Run methodology](https://developers.openai.com/cookbook/examples/audio/voice_agent_evaluation).
