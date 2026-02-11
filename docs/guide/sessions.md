# Sessions

Sessions enable conversation persistence across multiple interactions.

## Session Manager

Create a session manager with a SQLite database:

```python
from pathlib import Path
from nagents import SessionManager

session_manager = SessionManager(Path("sessions.db"))

agent = Agent(
    provider=provider,
    session_manager=session_manager,
)
```

## Using Sessions

Specify a `session_id` to maintain conversation history:

```python
# First interaction
async for event in agent.run(
    "My name is Alice",
    session_id="user-123",
):
    ...

# Later interaction (same session)
async for event in agent.run(
    "What's my name?",
    session_id="user-123",
):
    # Model remembers: "Your name is Alice"
    ...
```

## Auto-Generated Sessions

If no `session_id` is provided, a new session is created automatically:

```python
async for event in agent.run("Hello"):
    if isinstance(event, DoneEvent):
        print(f"Session ID: {event.session_id}")
        # Save this ID to continue the conversation later
```

## Session Data

Sessions store:

- Conversation messages (user, assistant, tool calls/results)
- Session metadata (created_at, updated_at, user_id)
- System prompt at session creation time

## User Association

Associate sessions with users:

```python
async for event in agent.run(
    "Hello",
    session_id="conversation-456",
    user_id="user-123",
):
    ...
```

## Session Lifecycle

```python
# Initialize session manager
await session_manager.initialize()

# Sessions are automatically created/updated during agent.run()

# Close when done
await session_manager.close()

# Or use agent.close() which handles this
await agent.close()
```

## Multiple Sessions

Handle multiple concurrent sessions:

```python
# User A's conversation
async for event in agent.run("Hello", session_id="user-a-session"):
    ...

# User B's conversation (completely separate)
async for event in agent.run("Hello", session_id="user-b-session"):
    ...
```
