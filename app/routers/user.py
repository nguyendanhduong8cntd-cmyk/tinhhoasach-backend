"""POST /v1/user (upsert, idempotent) + POST /v1/events (batch log)."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import Event, get_db, now_ms
from ..deps import require_api_key
from ..entitlement import assign_pricing_tier, get_or_create_user, refresh_entitlement
from ..envelope import ok, status_only
from ..schemas import EventsBatch, UserUpsert

router = APIRouter(prefix="/v1", tags=["user"], dependencies=[Depends(require_api_key)])


@router.post("/user")
def upsert_user(body: UserUpsert, db: Session = Depends(get_db)):
    lang = body.language[0] if body.language else "vi"
    user = get_or_create_user(db, body.user_id, language=lang, region=body.device_region)
    # keep mutable profile fields fresh; entitlement fields are owned by billing
    user.language = lang
    user.region = body.device_region
    if not user.pricing_tier:
        user.pricing_tier = assign_pricing_tier(user.uid)
    db.commit()
    refresh_entitlement(db, user)  # lazy-refill safety net on every /user call
    return status_only()


@router.post("/events")
def log_events(body: EventsBatch, db: Session = Depends(get_db)):
    for e in body.events:
        db.add(Event(
            event_name=e.event_name,
            user_id=e.user_id,
            session_id=e.session_id,
            event_time=e.event_time or now_ms(),
            properties=e.properties,
        ))
    db.commit()
    return status_only()
