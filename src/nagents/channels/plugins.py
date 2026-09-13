"""Explicit discovery of third-party ``nagents.channels`` entry points."""

from importlib.metadata import entry_points
from typing import TYPE_CHECKING
from typing import cast

from .types import Channel
from .types import ChannelValue

if TYPE_CHECKING:
    from collections.abc import Callable


def load_channel(name: str, config: dict[str, ChannelValue]) -> Channel:
    """Load one installed, explicitly selected factory; never auto-enable plugins.

    A factory receives a JSON-compatible configuration dictionary and returns a
    Channel. Constructors/factories must defer network I/O until ``open``.
    """
    matches = tuple(entry_points(group="nagents.channels", name=name))
    if len(matches) != 1:
        raise ValueError(f"Expected one installed channel plugin named {name!r}; found {len(matches)}")
    factory = matches[0].load()
    if not callable(factory):
        raise TypeError("Channel entry point must be a callable factory")
    channel = cast("Callable[[dict[str, ChannelValue]], Channel]", factory)(config)
    if not isinstance(channel, Channel):
        raise TypeError("Channel factory must return a Channel")
    return channel
