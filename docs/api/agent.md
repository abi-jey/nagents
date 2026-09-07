# Agent API

`Agent` coordinates provider generation, conversation history, and callable
tools. Import it from `nagents`. The reference below is generated from the
current source checkout, not an assertion that every API is in a published
release. See [Basic Agent](../examples/basic-agent.md) for a runnable example.

Pass generation options through `run(config=GenerationConfig(...))`, not the
agent constructor. Streaming is opt-in. Close the agent when finished, and
explicitly close a run iterator if you stop consuming it early. Serialize runs
and compaction for the same session.

::: nagents.Agent
    options:
      members:
        - __init__
        - initialize
        - run
        - run_simple
        - register_tool
        - clear_session
        - close

## Text-Run Extensions

These hooks are trusted Python code, not a sandbox. Their supported modes and
failure semantics are described in the [ngn guide](../guide/ngn.md).

::: nagents.AgentPlugin

::: nagents.CompactionStrategy
