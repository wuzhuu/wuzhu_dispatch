from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from src.utils.config import load_settings


PROXY_ENV_KEYS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
)


def disable_proxy_if_configured() -> None:
    settings = load_settings()
    if not settings.get("collector", {}).get("disable_proxy", False):
        return
    for key in PROXY_ENV_KEYS:
        os.environ.pop(key, None)


@contextmanager
def proxy_disabled() -> Iterator[None]:
    old_values = {key: os.environ.get(key) for key in PROXY_ENV_KEYS}
    try:
        disable_proxy_if_configured()
        yield
    finally:
        for key, value in old_values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
