"""Remote presentation labels must not become connection routing authority."""

from nagents.harness.agent_identity import agent_identities
from nagents.harness.config import AgentProfile
from nagents.harness.remote.profiles import RemoteProfile
from nagents.harness.remote.store import RemoteAgentMetadata


def test_remote_identity_uses_card_metadata_and_keeps_connection_note_private() -> None:
    remote = AgentProfile(description="Old desktop note", a2a=RemoteProfile(url="https://peer.example"))
    metadata = RemoteAgentMetadata(remote.a2a.fingerprint, "Media Manager", "Manage media", 0)
    identities = agent_identities(
        {"assistant": AgentProfile(description="Local assistant"), "desktop-experiment": remote},
        {"desktop-experiment": metadata},
    )
    assert identities["desktop-experiment"].name == "Media Manager"
    assert identities["desktop-experiment"].description == "Manage media"
    assert identities["desktop-experiment"].remote
    assert identities["assistant"].name == "assistant"
    assert identities["assistant"].description == "Local assistant"
    assert remote.description == "Old desktop note"


def test_missing_metadata_does_not_reveal_the_connection_alias_or_note() -> None:
    identities = agent_identities(
        {
            "legacy-desktop": AgentProfile(
                description="Legacy description", a2a=RemoteProfile(url="https://peer.example")
            )
        },
        {},
    )
    assert identities["legacy-desktop"].name == "Remote agent"
    assert identities["legacy-desktop"].description == "Remote agent"
    stale = RemoteAgentMetadata("0" * 64, "A different endpoint", "Stale metadata", 0)
    assert identities == agent_identities(
        {
            "legacy-desktop": AgentProfile(
                description="Legacy description", a2a=RemoteProfile(url="https://peer.example")
            )
        },
        {"legacy-desktop": stale},
    )


def test_colliding_remote_labels_cannot_shadow_local_or_connection_routing_keys() -> None:
    remote = AgentProfile(a2a=RemoteProfile(url="https://peer.example"))
    profiles = {"assistant": AgentProfile(), "first-alias": remote, "second-alias": remote, "third-alias": remote}
    metadata = {
        "first-alias": RemoteAgentMetadata(remote.a2a.fingerprint, "assistant", "", 0),
        "second-alias": RemoteAgentMetadata(remote.a2a.fingerprint, "ASSISTANT", "", 0),
        "third-alias": RemoteAgentMetadata(remote.a2a.fingerprint, "first-alias", "", 0),
    }
    identities = agent_identities(profiles, metadata)
    names = [identity.name.casefold() for identity in identities.values()]
    assert len(names) == len(set(names))
    assert identities["assistant"].name == "assistant"
    assert all(identities[key].name not in profiles for key in metadata)
    assert identities == agent_identities(dict(reversed(list(profiles.items()))), metadata)
