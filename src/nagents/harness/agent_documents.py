"""Canonical agent Markdown locations and generated defaults."""

from __future__ import annotations

import hashlib
import logging
import os
from importlib import resources
from typing import TYPE_CHECKING
from typing import Literal

from .model_bindings import AgentModelStore

if TYPE_CHECKING:
    from .providers import ScopedProviderRegistryStore

DocumentScope = Literal["global", "workspace", "agent"]
logger = logging.getLogger(__name__)


def agent_document_store(providers: ScopedProviderRegistryStore, scope: DocumentScope) -> AgentModelStore:
    if scope == "agent":
        return providers.agent_models
    return AgentModelStore(providers.model_store(scope).path.parent, disable_auto_update=scope == "global")


def bundled_assistant() -> tuple[dict[str, object], str]:
    source = resources.files("nagents.harness").joinpath("instructions", "assistant.md").read_bytes()
    return AgentModelStore._preferences(source), hashlib.sha256(source).hexdigest()


def ensure_agent_documents(providers: ScopedProviderRegistryStore) -> None:
    """Run at startup. UI writes disable management; external true flags remain authoritative."""
    from .providers import _exclusive_lock

    store = AgentModelStore(providers.model_store("global").path.parent)
    store._check_directory()
    store.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = store.directory / ".assistant-init.lock"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Assistant initialization lock must be a regular file")
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "a+b") as stream, _exclusive_lock(stream):
        _initialize_documents(providers)


def _initialize_documents(providers: ScopedProviderRegistryStore) -> None:
    store = AgentModelStore(providers.model_store("global").path.parent)
    current = store.load("assistant")
    defaults, revision = bundled_assistant()
    if current.revision == "0" * 64:
        store.patch("assistant", {**defaults, "default_revision": revision}, expected=current.revision)
    elif current.preferences.get("auto_update") is True and current.preferences.get("default_revision") != revision:
        logger.warning(
            "Bundled assistant defaults changed; replacing auto_update-enabled global assistant file: %s. "
            "Set auto_update: false to preserve customizations.",
            current.path,
        )
        store.replace_fields(
            "assistant",
            tuple(current.preferences.keys() | defaults.keys() | {"default_revision"}),
            {**defaults, "default_revision": revision},
            expected=current.revision,
        )
