---
name: assistant
tools: [read_file, list_files, to_agent, agent_status]
allowed_agents: [helper]
live:
  enabled: true
  model:
    provider: local-codex
    id: gpt-live-1-codex
  voice: sol
  context_mode: none
---
