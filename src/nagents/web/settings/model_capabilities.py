"""Short-lived catalog choices bound to saved routes and local credentials."""

from __future__ import annotations

import hashlib
import json
import os
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from time import monotonic
from time import time
from typing import TYPE_CHECKING

from nagents.harness.connection import codex_discovery_selected
from nagents.provider.context import MAX_METADATA_MODELS
from nagents.provider.context import METADATA_TTL
from nagents.provider.openai import _load_config
from nagents.provider.openai import _local_credentials

if TYPE_CHECKING:
    from nagents.harness.auth import OpenAIAuth
    from nagents.harness.model_catalog import ModelCatalog
    from nagents.harness.providers import ProviderProfile
    from nagents.types import ReasoningEffort

MAX_CATALOG_CONNECTIONS = 128


@dataclass(frozen=True)
class CatalogRoute:
    profile: ProviderProfile
    credentials: str = field(repr=False)


def catalog_route(profile: ProviderProfile, auth: OpenAIAuth) -> CatalogRoute:
    """Hash the selected local source; never refresh tokens or contact a provider."""
    identity: tuple[str, ...] = ()
    try:
        if profile.auth == "entra":
            # An opaque SDK credential chain cannot be verified by a local read.
            # Keep its metadata request-local instead of retaining cross-account choices.
            pass
        elif profile.kind == "openai" and (profile.auth == "chatgpt" or (profile.auth == "auto" and auth.logged_in())):
            tokens = auth._load()
            if tokens is not None and tokens.expires_at > time():
                identity = ("chatgpt", tokens.access_token, tokens.id_token, str(tokens.expires_at))
        elif codex_discovery_selected(profile):
            configured = _load_config()
            if configured.oauth:
                credentials = _local_credentials(configured.home)
                identity = (
                    "codex",
                    credentials.access_token,
                    credentials.account_id,
                    credentials.residency,
                    configured.workspace,
                )
            else:
                identity = ("codex-api", configured.api, configured.base_url, configured.api_key)
        elif key := os.environ.get(profile.key_env, ""):
            identity = ("environment", profile.key_env, key)
    except (OSError, ValueError, RuntimeError):
        # An inaccessible or changed login is a cache miss, never authority to
        # reuse choices from another credential source or disclose its contents.
        pass
    fingerprint = hashlib.sha256(json.dumps(identity).encode()).hexdigest() if identity else ""
    return CatalogRoute(replace(profile, model_context_windows=dict(profile.model_context_windows)), fingerprint)


@dataclass(frozen=True)
class CatalogChoices:
    tracked: bool = False
    known: bool = False
    options: tuple[ReasoningEffort, ...] = ()
    refresh_required: bool = False


@dataclass(frozen=True)
class _Entry:
    route: CatalogRoute
    expires: float
    efforts: dict[str, tuple[ReasoningEffort, ...]]


class CatalogCapabilities:
    def __init__(self) -> None:
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    def remember(self, scope: str, name: str, route: CatalogRoute, catalog: ModelCatalog) -> None:
        efforts = {
            model: tuple(details["reasoning_efforts"])
            for model in catalog.models[:MAX_METADATA_MODELS]
            if (details := catalog.metadata.get(model)) is not None and "reasoning_efforts" in details
        }
        key = (scope, name)
        self._entries[key] = _Entry(route, monotonic() + METADATA_TTL, efforts)
        self._entries.move_to_end(key)
        while len(self._entries) > MAX_CATALOG_CONNECTIONS:
            self._entries.popitem(last=False)

    def lookup(self, scope: str, name: str, route: CatalogRoute, model: str) -> CatalogChoices:
        entry = self._entries.get((scope, name))
        if entry is None:
            return CatalogChoices()
        if route != entry.route or monotonic() >= entry.expires:
            return CatalogChoices(tracked=True, refresh_required=True)
        self._entries.move_to_end((scope, name))
        return CatalogChoices(
            tracked=True,
            known=model in entry.efforts,
            options=entry.efforts.get(model, ()),
            refresh_required=not bool(route.credentials),
        )
