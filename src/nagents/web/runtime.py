"""Shared HTTP configuration for ordinary and development serving."""

from collections.abc import Callable

from fastapi import FastAPI
from uvicorn import Config

from .subscriptions import MAX_FRAME


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
    )
