"""Pydantic request models (responses are built as plain dicts via envelope.ok)."""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


# ── User & analytics ─────────────────────────────────────────────────
class Attribution(BaseModel):
    tracker_name: str = "Organic"
    network: str = "Organic"
    campaign: str = ""


class UserUpsert(BaseModel):
    user_id: str
    version: str = "1.0.0"
    language: list[str] = Field(default_factory=lambda: ["vi"])
    device_region: str = "VN"
    operating_system: str = "android"
    os_version: Optional[str] = None
    device_model: Optional[str] = None
    time_zone_offset_seconds: int = 25200
    attribution: Optional[Attribution] = None


class EventItem(BaseModel):
    event_name: str
    user_id: str
    session_id: Optional[str] = None
    event_time: Optional[int] = None
    properties: dict[str, Any] = Field(default_factory=dict)


class EventsBatch(BaseModel):
    events: list[EventItem]


# ── Per-user ─────────────────────────────────────────────────────────
class LibraryAction(BaseModel):
    book_id: str
    action: str = "save"                 # save | remove


class HighlightCreate(BaseModel):
    book_id: str
    chapter_index: int
    text: str
    color: str = "yellow"


class ProgressUpdate(BaseModel):
    user_id: Optional[str] = None
    book_id: str
    chapter_index: int = 0
    position: float = 0.0


# ── Billing ──────────────────────────────────────────────────────────
class VerifyRequest(BaseModel):
    user_id: str
    platform: str = "android"
    product_id: str                      # tier id | bucket | base plan | catalog id | lifetime
    purchase_token: str


class RestoreItem(BaseModel):
    product_id: str
    purchase_token: str


class RestoreRequest(BaseModel):
    user_id: str
    purchases: list[RestoreItem] = Field(default_factory=list)
