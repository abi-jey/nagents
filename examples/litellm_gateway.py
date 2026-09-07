"""API-key gateway example, with no LiteLLM SDK or credential-file loading.

Set NGN_BASE_URL to your gateway API prefix, NGN_MODEL to a configured model
alias, and LITELLM_API_KEY to its virtual key. NGN_API defaults to
chat_completions; responses, messages, and text-only completions also work.
"""

import asyncio
import os

from nagents.events import ErrorEvent
from nagents.events import TextDoneEvent
from nagents.events import ToolCallEvent
from nagents.provider import Provider
from nagents.provider import ProviderType
from nagents.types import Message
from nagents.types import ToolCall
from nagents.types import ToolDefinition


async def main() -> None:
    api = os.environ.get("NGN_API", "chat_completions")
    messages = [Message(role="user", content="Say hello. Use the greeting tool if available.")]
    tools = (
        []
        if api == "completions"
        else [ToolDefinition("greeting", "Return a greeting", {"type": "object", "properties": {}})]
    )
    async with Provider(
        ProviderType.LITELLM,
        api_key=os.environ["LITELLM_API_KEY"],
        model=os.environ["NGN_MODEL"],
        base_url=os.environ["NGN_BASE_URL"],
        api=api,
    ) as provider:
        for _ in range(3):
            text = ""
            calls: list[ToolCall] = []
            async for event in provider.generate(messages, tools):
                if isinstance(event, ErrorEvent):
                    print(event.message)
                    return
                if isinstance(event, TextDoneEvent):
                    text = event.text
                elif isinstance(event, ToolCallEvent):
                    calls.append(ToolCall(event.id, event.name, event.arguments, event.metadata))
            if not calls:
                print(text)
                return
            messages.append(Message(role="assistant", content=text or None, tool_calls=calls))
            for call in calls:
                result = (
                    "Hello from a local tool!"
                    if call.name == "greeting" and not call.arguments
                    else "Unsupported tool call"
                )
                messages.append(Message(role="tool", tool_call_id=call.id, name=call.name, content=result))
        print("Stopped at the example's three-request limit.")


if __name__ == "__main__":
    asyncio.run(main())
