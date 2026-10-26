"""Web agent conversations, receipts, activation history, and task routes."""

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .routes import TASK_DRAIN_TIMEOUT as TASK_DRAIN_TIMEOUT
    from .routes import TaskMessageInput as TaskMessageInput
    from .routes import TaskSettingsInput as TaskSettingsInput
    from .routes import TaskStartInput as TaskStartInput
    from .routes import Tasks as Tasks
    from .routes import register as register

__all__ = ["TASK_DRAIN_TIMEOUT", "TaskMessageInput", "TaskSettingsInput", "TaskStartInput", "Tasks", "register"]


def __getattr__(name: str) -> object:
    if name not in __all__:
        raise AttributeError(name)
    value: object = getattr(import_module(".routes", __name__), name)
    globals()[name] = value
    return value
