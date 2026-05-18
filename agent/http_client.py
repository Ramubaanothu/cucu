"""Shared HTTP client factory — picks up HTTP_PROXY / HTTPS_PROXY from env."""
from __future__ import annotations

import os

import httpx

_PROXY = (
    os.environ.get("HTTPS_PROXY")
    or os.environ.get("HTTP_PROXY")
    or os.environ.get("https_proxy")
    or os.environ.get("http_proxy")
    or None
)


def make_client(**kwargs) -> httpx.AsyncClient:
    """Return an AsyncClient that routes through HTTP_PROXY if set."""
    if _PROXY:
        kwargs.setdefault("proxy", _PROXY)
    return httpx.AsyncClient(**kwargs)
