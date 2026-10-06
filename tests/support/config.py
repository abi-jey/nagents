"""Explicit provider profiles for programmatic Harness fixtures."""

from nagents.harness.providers import ProviderProfile


def connection(
    kind: str = "openai",
    *,
    name: str = "",
    auth: str = "auto",
    api: str = "auto",
    base_url: str = "",
    api_key_env: str = "",
    api_version: str = "",
) -> dict[str, ProviderProfile]:
    return {
        name: ProviderProfile(
            kind=kind,
            auth=auth,
            api=api,
            base_url=base_url,
            api_key_env=api_key_env,
            api_version=api_version,
        )
    }
