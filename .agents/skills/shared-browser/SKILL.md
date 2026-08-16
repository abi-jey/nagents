---
name: shared-browser
description: Use when interacting with any webpage — browsing, reading, researching, clicking, filling forms, or working with web apps/websites. avoid using playright or other browser automation tools.
---

# Browser Setup

Source of truth for the browser workflow. Do not duplicate the learning and setup and issues in any other files.

- Dedicated Chrome profile: `.agents/chrome-context`
- Chrome runs with CDP on `http://127.0.0.1:9222`
- Google Chrome DevTools MCP configured in opencode as MCP server `chrome-devtools`, attaching to `http://127.0.0.1:9222`

## If Chrome DevTools MCP fails, are not available

1. Check whether the shared Chrome/CDP process is running: like `curl -sS http://127.0.0.1:9222/json/version`.
2. If not, relaunch the dedicated context:
   ```bash
   tmux new-session -d -s agent-chrome "google-chrome --remote-debugging-port=9222 --remote-allow-origins=* --user-data-dir=.agents/chrome-context --no-first-run --no-default-browser-check https://www.google.com"
   ```
3. Retry Chrome DevTools MCP `list_pages`.

## Security notes

- Use dedicated profile `.agents/chrome-context` in the current dir, not any other chrome profiles
- CDP/MCP can access cookies/session tokens/open tabs; keep localhost-only and disable when done.
- Do not store API keys/secrets in workspace docs.

