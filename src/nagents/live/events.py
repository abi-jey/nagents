"""GPT-Live server events; backend events retain their normal Nagents types."""

from dataclasses import dataclass
from dataclasses import field

from ..events import Event
from ..events import EventType


@dataclass
class LiveEvent(Event):
    type: EventType = field(default=EventType.LIVE)
    event_type: str = ""
    payload: dict[str, object] = field(default_factory=dict)
