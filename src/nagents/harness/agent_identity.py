"""Public agent identities, separate from private connection routing keys."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from .config import AgentProfile
    from .remote.store import RemoteAgentMetadata


@dataclass(frozen=True)
class AgentIdentity:
    name: str
    description: str
    remote: bool


def agent_identities(
    profiles: Mapping[str, AgentProfile], metadata: Mapping[str, RemoteAgentMetadata]
) -> dict[str, AgentIdentity]:
    """Keep remote labels untrusted and unambiguous without publishing aliases.

    A remote card can supply presentation text, never replace another profile's
    routing key. Colliding labels receive a stable opaque discriminator.
    """
    cards = {
        key: metadata[key]
        for key, profile in profiles.items()
        if profile.a2a.configured and key in metadata and metadata[key].fingerprint == profile.a2a.fingerprint
    }
    names: dict[str, str] = {}
    for key, profile in profiles.items():
        card = cards.get(key)
        names[key] = (card.name.strip() if card else "") or ("Remote agent" if profile.a2a.configured else key)
    counts = Counter(name.casefold() for name in names.values())
    reserved = {name.casefold() for name in (*profiles, *names.values())}
    identities: dict[str, AgentIdentity] = {}
    for key, profile in profiles.items():
        name = names[key]
        remote = profile.a2a.configured
        card = cards.get(key)
        description = (card.description.strip() if card else "") if remote else profile.description
        if remote and not description:
            description = "Remote agent"
        if remote and (
            counts[name.casefold()] > 1
            or any(name.casefold() == other.casefold() for other in profiles if other != key)
        ):
            digest = hashlib.sha256(f"{key}:{profile.a2a.fingerprint}".encode()).hexdigest()
            candidate = f"{name} [remote {digest[:8]}]"
            index = 1
            while candidate.casefold() in reserved:
                candidate = f"{name} [remote {digest[:8]}-{index}]"
                index += 1
            name = candidate
        reserved.add(name.casefold())
        identities[key] = AgentIdentity(name, description, remote)
    return identities
