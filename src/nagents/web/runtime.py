"""Shared HTTP configuration for ordinary and development serving."""

import copy
from collections.abc import Callable
from time import gmtime
from time import struct_time

from fastapi import FastAPI
from uvicorn import Config
from uvicorn.config import LOGGING_CONFIG
from uvicorn.logging import DefaultFormatter

from .subscriptions import MAX_FRAME


class UTCDefaultFormatter(DefaultFormatter):
    """Keep server and audited lifecycle log timestamps stable across hosts."""

    @staticmethod
    def converter(timestamp: float | None = None) -> struct_time:
        return gmtime(timestamp)


def server_logging_config() -> dict[str, object]:
    """Use Uvicorn's default handler for a small allowlist of safe lifecycle logs."""
    configuration = copy.deepcopy(LOGGING_CONFIG)
    formatters = configuration["formatters"]
    if not isinstance(formatters, dict):
        raise RuntimeError("Uvicorn logging configuration has no formatter map")
    default = formatters["default"]
    if not isinstance(default, dict):
        raise RuntimeError("Uvicorn default formatter configuration is invalid")
    default["()"] = UTCDefaultFormatter
    default["fmt"] = "%(asctime)s.%(msecs)03dZ " + str(default["fmt"])
    default["datefmt"] = "%Y-%m-%dT%H:%M:%S"
    default["use_colors"] = False

    loggers = configuration["loggers"]
    if not isinstance(loggers, dict):
        raise RuntimeError("Uvicorn logging configuration has no logger map")
    loggers.update(
        {
            "nagents.harness.agents.subagents": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "nagents.harness.remote.runtime": {"handlers": ["default"], "level": "INFO", "propagate": False},
            "nagents.harness.remote.credentials": {"handlers": ["default"], "level": "WARNING", "propagate": False},
        }
    )
    return configuration


def server_config(app: FastAPI | Callable[[], FastAPI], *, host: str, port: int, factory: bool = False) -> Config:
    # One process and one lifespan own every Harness resource. Never trust proxy headers.
    return Config(
        app,
        host=host,
        port=port,
        factory=factory,
        proxy_headers=False,
        access_log=False,
        timeout_graceful_shutdown=3,
        ws_max_size=MAX_FRAME,
        ws_max_queue=16,
        ws_per_message_deflate=False,
        log_config=server_logging_config(),
    )
