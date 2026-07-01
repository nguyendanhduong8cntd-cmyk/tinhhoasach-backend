"""GET /v1/config — entitlement + pricing + flags + free-daily in one round-trip."""
from __future__ import annotations

import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import Book, FreeDaily, get_db
from ..deps import require_api_key
from ..entitlement import get_or_create_user, refresh_entitlement
from ..envelope import ok
from ..remote_config import get_config

router = APIRouter(prefix="/v1", tags=["config"])


def _today() -> str:
    return datetime.date.today().isoformat()


def _free_daily_payload(db: Session) -> dict:
    row = db.get(FreeDaily, _today())
    if row is None:  # fall back to the most recent rotation
        row = db.execute(select(FreeDaily).order_by(FreeDaily.date.desc())).scalars().first()
    if row is None:
        return {"start_date": _today(), "books": []}
    books = []
    for i, bid in enumerate(row.book_ids or []):
        b = db.get(Book, bid)
        if b:
            books.append({"id": b.id, "name": b.title, "display_index": i})
    return {"start_date": row.date, "books": books}


@router.get("/config", dependencies=[Depends(require_api_key)])
def get_v1_config(
    uid: str = Query(...),
    version: str = Query("1.0.0"),
    language: str = Query("vi"),
    platform: str = Query("android"),
    country: str = Query("VN"),
    db: Session = Depends(get_db),
):
    user = get_or_create_user(db, uid, language=language, region=country)
    user = refresh_entitlement(db, user)
    cfg = get_config(db)

    payload = {
        "min_version": cfg["min_version"],
        "in_maintenance": cfg["in_maintenance"],
        "premium": bool(user.premium),                       # ★ entitlement (server-owned)
        "premium_expires_at": user.premium_expires_at,       # epoch ms or null
        "pricing_tier": user.pricing_tier or "t1",           # ★ cohort A/B tier
        "display_mode": cfg["display_mode"],
        "onboarding_flow": cfg["onboarding_flow"],
        "gift_flags": cfg["gift_flags"],
        "paywall_modifiers": cfg["paywall_modifiers"],
        "premium_product_id": cfg["premium_product_id"],
        "base_plans": cfg["base_plans"],                     # ★ release-*-plan, remote-config driven
        "iap_catalog": cfg["iap_catalog"],
        "free_daily_book_list": _free_daily_payload(db),
    }
    return ok(payload)
