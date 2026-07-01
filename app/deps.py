"""Shared request dependencies."""
from __future__ import annotations

from fastapi import Header

from .config import get_settings
from .envelope import ApiError


def require_api_key(authorization: str | None = Header(default=None)) -> bool:
    """Auth v1: a static per-app key in the raw ``Authorization`` header (spec §1)."""
    if not authorization or authorization != get_settings().app_api_key:
        raise ApiError(401, "Invalid API key")
    return True
