"""Optional Textual interface for the nagents harness."""

from .app import NagentsApp
from .timing import configure_timer_sleep

configure_timer_sleep()

__all__ = ["NagentsApp"]
