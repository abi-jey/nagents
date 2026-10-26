"""Allowlisted local Codex failure reasons, never upstream payloads or excerpts."""

from __future__ import annotations

# Matching complete, framework-owned messages keeps diagnostics useful without
# treating arbitrary exception/provider text as safe to log or show in the UI.
_REASONS: dict[str, tuple[str, ...]] = {
    "request_media": (
        "Codex supports images only in user messages; audio and documents are unsupported.",
        "Codex supports PNG, JPEG, WebP, and GIF images only.",
        "Codex supports user images and text, not audio or document attachments.",
    ),
    "request_history": (
        "A tool result is missing its call ID; repair the session history.",
        "Codex received an unsupported message role.",
        "Invalid assistant tool call in session history.",
    ),
    "request_effort": (
        "This reasoning effort is not supported by the selected provider and model; choose provider default.",
        "reasoning_effort must be default, none, minimal, low, medium, high, xhigh, or max",
    ),
    "response_failed": (
        "Codex reported a failed response; no tool calls were released. Retry the request.",
        "Codex response failed, was incomplete, or was cancelled; no tool calls were released. Retry the request.",
    ),
    "response_incomplete": (
        "Codex returned an incomplete response; no tool calls were released. Retry the request.",
        "Codex response was not completed; no tool calls were released.",
    ),
    "response_cancelled": ("Codex cancelled the response; no tool calls were released. Retry the request.",),
    "stream_truncated": (
        "Codex stream ended inside an event; retry the request.",
        "Codex stream ended before response.completed; no tool calls were released.",
    ),
    "stream_format": (
        "Codex did not return an event stream; retry the request.",
        "Codex returned malformed stream JSON; retry the request.",
        "Codex returned malformed response data; retry the request.",
        "Codex returned malformed response text; retry the request.",
        "Codex completed response has malformed output.",
        "Codex returned malformed message content.",
    ),
    "stream_limit": (
        "Codex response exceeded the safe stream size limit.",
        "Codex response exceeded the safe event size limit.",
        "Provider returned too many reasoning summary parts.",
    ),
    "output_identity": (
        "Codex returned an invalid output index.",
        "Codex returned inconsistent output item identities.",
        "Codex returned inconsistent argument item identities.",
        "Codex returned inconsistent text item identities.",
        "Codex returned inconsistent completed output identities.",
    ),
    "output_incomplete": (
        "Codex completed before streamed output finished; no tool calls were released.",
        "Codex completed response omitted streamed output; no tool calls were released.",
        "Codex returned an unfinished message.",
    ),
    "output_text": (
        "Codex sent text after a content part finished.",
        "Codex returned inconsistent completed text.",
        "Codex terminal output did not match streamed text.",
    ),
    "tool_arguments": (
        "Codex returned malformed tool arguments; no tool calls were released.",
        "Codex returned inconsistent completed tool arguments.",
        "Codex returned inconsistent tool arguments.",
        "Codex returned tool arguments without a complete tool call.",
    ),
    "tool_incomplete": ("Codex returned an invalid or unfinished tool call.",),
    "usage_invalid": ("Codex returned invalid token usage.",),
    "summary_invalid": (
        "Provider returned an invalid reasoning summary index.",
        "Provider returned inconsistent reasoning summary text.",
        "Provider sent reasoning text after its summary finished.",
    ),
}
_MESSAGE_REASONS = {message: reason for reason, messages in _REASONS.items() for message in messages}


def codex_protocol_reason(message: object) -> str:
    """Return one fixed reason token, or unknown; never return supplied text."""
    return _MESSAGE_REASONS.get(message, "unknown") if isinstance(message, str) else "unknown"
