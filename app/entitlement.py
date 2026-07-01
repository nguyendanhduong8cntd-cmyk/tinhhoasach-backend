"""Entitlement helpers: lazy-refill safety net + get-or-create user."""
from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from .db import User, atomic_expire_sub, now_ms


def assign_pricing_tier(uid: str) -> str:
    """Deterministic cohort split (server-driven A/B). Stable per uid."""
    h = int(hashlib.sha256(uid.encode()).hexdigest(), 16)
    return "t1" if h % 2 == 0 else "t2"


def get_or_create_user(db: Session, uid: str, *, language: str = "vi", region: str = "VN") -> User:
    user = db.get(User, uid)
    if user is None:
        user = User(
            uid=uid, created_at=now_ms(), premium=False,
            pricing_tier=assign_pricing_tier(uid), language=language, region=region,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def refresh_entitlement(db: Session, user: User) -> User:
    """Lazy refill (playbook §6): drop premium if a timed sub has expired.

    Safety net for a missed EXPIRED RTDN. Lifetime (premium & expires_at IS NULL)
    is never touched.
    """
    if user.premium and user.premium_expires_at is not None and user.premium_expires_at <= now_ms():
        atomic_expire_sub(db, uid=user.uid)
        db.commit()
        db.refresh(user)
    return user
