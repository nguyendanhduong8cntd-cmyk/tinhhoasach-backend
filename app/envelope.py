"""Status-envelope helpers + the ApiError exception (spec §9)."""
from __future__ import annotations

from typing import Any


def ok(payload: Any = None) -> dict:
    """Wrap a successful payload: {status:{code,message}, payload:{...}}."""
    body: dict = {"status": {"code": 200, "message": "OK"}}
    if payload is not None:
        body["payload"] = payload
    return body


def status_only(code: int = 200, message: str = "OK") -> dict:
    return {"status": {"code": code, "message": message}}


class ApiError(Exception):
    """Rendered by the handler in main.py as the spec's error envelope.

    Pass ``raw`` to emit a non-standard body (e.g. 499 {"errorText": ...}).
    """

    def __init__(self, code: int, message: str | None = None, raw: dict | None = None):
        self.code = code
        self.message = message or ""
        self.raw = raw
        super().__init__(f"{code}: {self.message}")
