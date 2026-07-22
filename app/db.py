"""SQLAlchemy models + the idempotency ledger + atomic helpers.

Portable across SQLite (zero-config local) and Postgres (prod) via
dialect-aware ``INSERT ... ON CONFLICT DO NOTHING``. Every entitlement
mutation is a guarded single ``UPDATE ... WHERE`` — never read-then-write
a balance, so redeliveries and concurrent claims stay correct (playbook §2).
"""
from __future__ import annotations

import time
from typing import Iterator, Optional

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    select,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from .config import get_settings

Base = declarative_base()
_settings = get_settings()

_connect_args = {"check_same_thread": False} if _settings.database_url.startswith("sqlite") else {}
engine = create_engine(_settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


def now_ms() -> int:
    return int(time.time() * 1000)


# ── Models (spec §7) ─────────────────────────────────────────────────
class User(Base):
    __tablename__ = "users"
    uid = Column(String, primary_key=True)
    created_at = Column(BigInteger, default=now_ms)
    premium = Column(Boolean, default=False, nullable=False)           # ★ entitlement
    premium_expires_at = Column(BigInteger, nullable=True)             # epoch ms; NULL=lifetime/free
    sub_type = Column(String, nullable=True)                          # weekly_pro/monthly_pro/yearly_pro/lifetime_pro
    purchase_token = Column(String, nullable=True)
    pricing_tier = Column(String, default="t1")
    language = Column(String, default="vi")
    region = Column(String, default="VN")
    current_streak = Column(Integer, default=0)
    best_streak = Column(Integer, default=0)
    last_open_date = Column(String, nullable=True)                     # 'YYYY-MM-DD'


class Purchase(Base):
    """The idempotency ledger — one row per purchase_token (grant at most once)."""
    __tablename__ = "purchases"
    purchase_token = Column(String, primary_key=True)                 # UNIQUE race guard
    uid = Column(String, index=True)
    sku = Column(String)
    platform = Column(String, default="android")
    granted_at = Column(BigInteger, default=now_ms)                   # last grant time (renewal idempotency)
    total_granted = Column(Integer, default=0)                        # grant events (initial + renewals) — clawback
    revoked = Column(Boolean, default=False, nullable=False)
    email_at_grant = Column(String, nullable=True)                    # leaked-token defense (§7.2)


class Book(Base):
    __tablename__ = "books"
    id = Column(String, primary_key=True)
    title = Column(String)
    author = Column(String)
    cover_url = Column(String)
    description = Column(Text)
    category = Column(JSON)                                           # list[str]
    insights = Column(JSON)                                           # list[str] — key takeaways
    duration_min = Column(Integer)
    chapter_count = Column(Integer)
    pro_only = Column(Boolean, default=True)
    rating = Column(Float)
    lang = Column(String, default="vi")


class Chapter(Base):
    __tablename__ = "chapters"
    id = Column(String, primary_key=True)
    book_id = Column(String, ForeignKey("books.id"), index=True)
    idx = Column(Integer)
    title = Column(String)
    text_md = Column(Text)
    audio_path = Column(String)                                      # storage path; signed at read time


class Translation(Base):
    """Lazy cache of AI-translated book fields (Tổng quan + Ý tưởng chính) per (book, language).

    The catalogue is stored in one source language; when the app requests a book in another
    language we translate ``description`` + ``insights`` via Gemini ONCE and store the result here,
    so every later open in that language is an instant DB read (no repeat LLM call / latency)."""
    __tablename__ = "translations"
    book_id = Column(String, primary_key=True)
    lang = Column(String, primary_key=True)      # canonical target code e.g. 'vi','es','zh-TW','pt-BR'
    description = Column(Text)
    insights = Column(JSON)                       # list[str]
    created_at = Column(BigInteger, default=now_ms)


class Category(Base):
    __tablename__ = "categories"
    id = Column(String, primary_key=True)
    name = Column(String)
    icon = Column(String)
    book_count = Column(Integer, default=0)


class Library(Base):
    __tablename__ = "library"
    uid = Column(String, primary_key=True)
    book_id = Column(String, primary_key=True)
    saved_at = Column(BigInteger, default=now_ms)


class Highlight(Base):
    __tablename__ = "highlights"
    id = Column(String, primary_key=True)
    uid = Column(String, index=True)
    book_id = Column(String)
    chapter_index = Column(Integer)
    text = Column(Text)
    color = Column(String, default="yellow")
    created_at = Column(BigInteger, default=now_ms)


class Progress(Base):
    __tablename__ = "progress"
    uid = Column(String, primary_key=True)
    book_id = Column(String, primary_key=True)
    chapter_index = Column(Integer, default=0)
    position = Column(Float, default=0.0)
    updated_at = Column(BigInteger, default=now_ms)


class ConfigCohort(Base):
    __tablename__ = "config_cohorts"
    cohort_key = Column(String, primary_key=True)
    pricing_tier = Column(String)
    gift_flags = Column(JSON)
    paywall_modifiers = Column(JSON)


class FreeDaily(Base):
    __tablename__ = "free_daily"
    date = Column(String, primary_key=True)                         # 'YYYY-MM-DD'
    book_ids = Column(JSON)                                         # list[str]


class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    event_name = Column(String, index=True)
    user_id = Column(String, index=True)
    session_id = Column(String)
    event_time = Column(BigInteger)
    properties = Column(JSON)


class RemoteConfig(Base):
    """Server-driven config: change pricing/flags/base-plans without an app update."""
    __tablename__ = "remote_config"
    key = Column(String, primary_key=True)
    value = Column(JSON)


# ── Session / bootstrap ──────────────────────────────────────────────
def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)


# ── Atomic helpers (all single guarded statements) ───────────────────
def _insert_or_ignore(db: Session, model, values: dict, conflict_cols: list[str]) -> bool:
    """Dialect-aware INSERT ... ON CONFLICT DO NOTHING. True => row inserted."""
    ins = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    stmt = ins(model).values(**values).on_conflict_do_nothing(index_elements=conflict_cols)
    return db.execute(stmt).rowcount == 1


def atomic_claim_purchase(db: Session, *, token: str, uid: str, sku: str,
                          platform: str = "android", email_at_grant: Optional[str] = None) -> bool:
    """Claim the token exactly once. True => first grant; False => replay.

    This is BOTH the replay guard (Pub/Sub at-least-once) and the concurrency
    race guard between two simultaneous claims of the same valid token.
    """
    return _insert_or_ignore(db, Purchase, {
        "purchase_token": token, "uid": uid, "sku": sku, "platform": platform,
        "granted_at": now_ms(), "total_granted": 0, "revoked": False,
        "email_at_grant": email_at_grant,
    }, ["purchase_token"])


def atomic_grant_sub(db: Session, *, uid: str, sku: str, expiry_ms: Optional[int],
                     purchase_token: str) -> None:
    """Flip premium ON. Never shrink VIP; never downgrade a lifetime to a timed sub.

    ``expiry_ms=None`` grants lifetime.
    """
    user = db.get(User, uid)
    is_lifetime_now = bool(
        user and user.premium and user.premium_expires_at is None and user.sub_type == "lifetime_pro"
    )
    if expiry_ms is None:  # granting lifetime
        db.execute(update(User).where(User.uid == uid).values(
            premium=True, premium_expires_at=None, sub_type=sku, purchase_token=purchase_token))
        return
    if is_lifetime_now:  # keep the stronger lifetime entitlement
        return
    new_expiry = expiry_ms
    if user and user.premium_expires_at is not None:
        new_expiry = max(user.premium_expires_at, expiry_ms)  # never shrink
    db.execute(update(User).where(User.uid == uid).values(
        premium=True, premium_expires_at=new_expiry, sub_type=sku, purchase_token=purchase_token))


def atomic_expire_sub(db: Session, *, uid: str) -> int:
    """Drop entitlement idempotently (WHERE sub_type IS NOT NULL). Returns rowcount."""
    res = db.execute(update(User).where(User.uid == uid, User.sub_type.isnot(None)).values(
        premium=False, sub_type=None, purchase_token=None))
    return res.rowcount


def set_premium_expired_now(db: Session, *, uid: str) -> None:
    """Chargeback/void clawback: kill VIP, stamp expiry = now."""
    db.execute(update(User).where(User.uid == uid).values(
        premium=False, premium_expires_at=now_ms(), sub_type=None, purchase_token=None))


def drop_premium_keep_sub(db: Session, *, uid: str) -> None:
    """ON_HOLD: drop VIP but keep sub_type so RECOVERED can re-entitle."""
    db.execute(update(User).where(User.uid == uid).values(premium=False))


def atomic_revoke_purchase(db: Session, *, token: str) -> Optional[int]:
    """Mark revoked at most once. Returns total_granted, or None if already revoked/absent.

    Guarded by ``WHERE revoked=0`` so two concurrent VOIDED redeliveries can't both claw back.
    """
    row = db.execute(
        select(Purchase).where(Purchase.purchase_token == token, Purchase.revoked.is_(False))
    ).scalar_one_or_none()
    if row is None:
        return None
    total = row.total_granted
    res = db.execute(update(Purchase).where(
        Purchase.purchase_token == token, Purchase.revoked.is_(False)).values(revoked=True))
    return total if res.rowcount == 1 else None


def atomic_bump_purchase_grant(db: Session, *, token: str) -> None:
    """Record a grant event (initial or renewal) + refresh last-grant time."""
    db.execute(update(Purchase).where(Purchase.purchase_token == token).values(
        total_granted=Purchase.total_granted + 1, granted_at=now_ms()))
