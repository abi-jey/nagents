"""Wire diagnostics retain routing evidence without recording credentials."""

import pytest

from nagents.observation import headers


@pytest.mark.parametrize(
    "name",
    [
        "Authorization",
        "Proxy-Authorization",
        "Cookie",
        "Set-Cookie",
        "X-API-Key",
        "api_key",
        "ApiKey",
        "X-Auth-Token",
        "X-Access-Token",
        "Ocp-Apim-Subscription-Key",
        "X-Client-Secret",
        "X-Password",
        "X-Credentials",
        "ApiKeyValue",
        "X-AccessKey",
        "X-ApiKeyId",
        "X-Custom-Routing",
        "Unexpected",
    ],
)
def test_credential_headers_are_redacted(name: str) -> None:
    assert headers({name: "private-value"}) == {name: "[redacted]"}


@pytest.mark.parametrize(
    "name",
    [
        "Session-Id",
        "X-Session-ID",
        "ChatGPT-Account-Id",
        "X-Request-ID",
        "x-openai-internal-codex-residency",
        "OpenAI-Beta",
        "originator",
        "traceparent",
        "Thread-Id",
        "Content-Type",
        "Cache-Control",
    ],
)
def test_noncredential_headers_are_preserved(name: str) -> None:
    assert headers({name: "diagnostic-value"}) == {name: "diagnostic-value"}


def test_mixed_headers_preserve_names_and_do_not_mutate_input() -> None:
    original = {"Authorization": "private", "X-Session-ID": "conversation-1"}
    assert headers(original) == {"Authorization": "[redacted]", "X-Session-ID": "conversation-1"}
    assert original["Authorization"] == "private"
