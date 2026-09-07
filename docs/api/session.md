# Session API

`SessionManager` stores conversations in SQLite. `Agent.run()` creates a session
automatically and accepts a string `session_id` for later turns. For manual
creation, use `get_or_create_session(session_id, user_id)`, which returns the ID,
not a session object. `list_sessions()` returns dictionaries.

The methods below follow the current source. They are storage operations, not
an authorization boundary; applications must enforce user access and serialize
operations on a shared session. See [Sessions](../guide/sessions.md).

::: nagents.SessionManager
    options:
      show_docstring_examples: false
      members:
        - __init__
        - initialize
        - get_or_create_session
        - session_exists
        - get_history
        - add_message
        - replace_context
        - list_sessions
        - get_message_count
        - clear_session
        - delete_session
