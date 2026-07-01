"""Billing spine (spec §6, playbook §3-§7): verify, restore, RTDN webhook.

Invariants ported verbatim from the playbook:
  * verify-then-claim ordering (a forged token never pollutes the ledger),
  * UNIQUE-token idempotency (grant at most once across both delivery paths),
  * tier re-verification (read the REAL base plan back out; reject spoof),
  * email-gated leaked-token migration (anonymous v1 => refuse cross-device),
  * RTDN fail-closed JWT + 64KB body cap + always-200,
  * linkedPurchaseToken revoke on upgrade/downgrade.
"""
from __future__ import annotations

import base64
import json

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from .. import play_billing as pb
from ..config import get_settings
from ..db import (
    Purchase, SessionLocal, User, atomic_bump_purchase_grant, atomic_claim_purchase,
    atomic_expire_sub, atomic_grant_sub, atomic_revoke_purchase, drop_premium_keep_sub,
    get_db, now_ms, set_premium_expired_now,
)
from ..deps import require_api_key
from ..entitlement import get_or_create_user
from ..envelope import ApiError
from ..remote_config import base_plan_ids
from ..schemas import RestoreRequest, VerifyRequest
from ..skus import (
    LIFETIME_SKU, duration_ms, is_lifetime, sub_for, tier_for_base_plan,
)

router = APIRouter(tags=["billing"])
_settings = get_settings()


# ── shared helpers ───────────────────────────────────────────────────
def _entitlement_response(user: User, note: str = "OK") -> dict:
    return {
        "status": {"code": 200, "message": note},
        "premium": bool(user.premium),
        "premium_expires_at": user.premium_expires_at,
        "sku": user.sub_type,
    }


def _emails_match(a: str | None, b: str | None) -> bool:
    return bool(a) and bool(b) and a == b


def _revoke_linked_token(db: Session, linked_token: str) -> None:
    """Upgrade/downgrade: claw back the OLD token so the user never holds two subs."""
    atomic_revoke_purchase(db, token=linked_token)
    old = db.get(Purchase, linked_token)
    if old:
        owner = db.get(User, old.uid)
        if owner and owner.purchase_token == linked_token:
            atomic_expire_sub(db, uid=owner.uid)
    db.expire_all()  # so a stale ORM row can't flush back and undo the clawback


# ── POST /v1/purchase/verify ─────────────────────────────────────────
@router.post("/v1/purchase/verify", dependencies=[Depends(require_api_key)])
def verify_purchase(body: VerifyRequest, db: Session = Depends(get_db)):
    user = get_or_create_user(db, body.user_id)
    token = body.purchase_token

    if is_lifetime(body.product_id):
        verified = pb.verify_one_time_purchase(token, body.product_id)
        if not pb.is_purchase_state_purchased(verified):
            raise ApiError(499, raw={"errorText": "No receipt data found"})
        _grant_from_claim(db, user, token=token, sku=LIFETIME_SKU, expiry_ms=None,
                          platform=body.platform, verified=None, product_id=body.product_id)
        db.refresh(user)
        return _entitlement_response(user)

    # Subscription: ALWAYS pre-verify (never trust product_id).
    base_plans = base_plan_ids(db)
    claimed = sub_for(body.product_id, base_plans)
    if claimed is None:
        raise ApiError(400, "Unknown product")

    verified = pb.verify_subscription_purchase(
        token, hint_base_plan=base_plans.get(claimed.bucket),
        hint_duration_days=claimed.duration_days)
    if not pb.is_purchase_state_purchased(verified):
        raise ApiError(499, raw={"errorText": "No receipt data found"})

    # Tier re-verification (anti-spoof): read the REAL base plan back out.
    real_tier = tier_for_base_plan(pb.get_base_plan_id(verified), base_plans)
    if real_tier and claimed.sku != real_tier.sku:
        raise ApiError(409, "Tier mismatch")
    tier = real_tier or claimed
    expiry = pb.get_expiry_epoch(verified)

    _grant_from_claim(db, user, token=token, sku=tier.sku, expiry_ms=expiry,
                      platform=body.platform, verified=verified)
    db.refresh(user)
    return _entitlement_response(user)


def _grant_from_claim(db: Session, user: User, *, token: str, sku: str,
                      expiry_ms: int | None, platform: str, verified: dict | None,
                      product_id: str | None = None) -> None:
    """Claim the token then grant. Handles fresh / same-user replay / cross-device."""
    fresh = atomic_claim_purchase(db, token=token, uid=user.uid, sku=sku,
                                  platform=platform, email_at_grant=None)
    if fresh:
        if verified:  # subscription
            linked = pb.get_linked_purchase_token(verified)
            if linked:
                _revoke_linked_token(db, linked)
            pb.acknowledge_subscription(token)
        else:  # one-time (lifetime): ack needs the real Play product id
            pb.acknowledge_one_time(token, product_id or sku)
        atomic_grant_sub(db, uid=user.uid, sku=sku, expiry_ms=expiry_ms, purchase_token=token)
        atomic_bump_purchase_grant(db, token=token)
        db.commit()
        return

    # Replay: token already in the ledger.
    existing = db.get(Purchase, token)
    if existing and existing.uid == user.uid:
        # Same user re-opening the app: re-apply Play's current state.
        atomic_grant_sub(db, uid=user.uid, sku=sku, expiry_ms=expiry_ms, purchase_token=token)
        db.commit()
        return
    # Cross-device migration is email-gated (playbook §7.2). v1 is anonymous
    # (no verified email either side) => refuse; the legit owner restores by signing in.
    if not _emails_match(existing.email_at_grant if existing else None, None):
        return  # do NOT grant Pro to this uid


# ── POST /v1/purchase/restore ────────────────────────────────────────
@router.post("/v1/purchase/restore", dependencies=[Depends(require_api_key)])
def restore_purchases(body: RestoreRequest, db: Session = Depends(get_db)):
    user = get_or_create_user(db, body.user_id)
    base_plans = base_plan_ids(db)

    best = None  # (sku, expiry, token, rank)
    for item in body.purchases[:8]:  # cap so a huge body can't burn Play API quota
        if is_lifetime(item.product_id):
            v = pb.verify_one_time_purchase(item.purchase_token, item.product_id)
            if pb.is_purchase_state_purchased(v):
                best = (LIFETIME_SKU, None, item.purchase_token, 99)
            continue
        claimed = sub_for(item.product_id, base_plans)
        v = pb.verify_subscription_purchase(
            item.purchase_token,
            hint_base_plan=base_plans.get(claimed.bucket) if claimed else None,
            hint_duration_days=claimed.duration_days if claimed else 7)
        if not pb.is_purchase_state_purchased(v):
            continue
        real_tier = tier_for_base_plan(pb.get_base_plan_id(v), base_plans)
        if claimed and real_tier and claimed.sku != real_tier.sku:
            continue  # drop tier-mismatch
        tier = real_tier or claimed
        if tier is None:
            continue
        rank = tier.rank
        if best is None or rank > best[3]:
            best = (tier.sku, pb.get_expiry_epoch(v), item.purchase_token, rank)

    if best:
        sku, expiry, token, _ = best
        # Ledger idempotency resumes; grant syncs current state (credits not re-granted).
        atomic_claim_purchase(db, token=token, uid=user.uid, sku=sku, email_at_grant=None)
        atomic_grant_sub(db, uid=user.uid, sku=sku, expiry_ms=expiry, purchase_token=token)
        db.commit()
        db.refresh(user)
    return _entitlement_response(user)


# ── POST /webhook/play-billing (Pub/Sub RTDN — source of truth) ──────
def _ok200() -> JSONResponse:
    return JSONResponse(status_code=200, content={"status": {"code": 200, "message": "OK"}})


async def _read_capped(request: Request, cap: int = 65536) -> bytes | None:
    """Streamed body cap (don't trust Content-Length; chunked can smuggle MBs)."""
    total = b""
    async for chunk in request.stream():
        total += chunk
        if len(total) > cap:
            return None
    return total


@router.post("/webhook/play-billing")
async def play_billing_webhook(request: Request, authorization: str = Header(default=None)):
    # 1. Verify the Pub/Sub bearer JWT FIRST (fail closed). Always 200 to avoid retry-loop.
    if not pb.verify_pubsub_jwt(authorization):
        return _ok200()
    # 2. Body cap (64KB) via streamed counter.
    body = await _read_capped(request, 65536)
    if body is None:
        return _ok200()
    try:
        envelope = json.loads(body)
        data_b64 = ((envelope or {}).get("message") or {}).get("data")
        if not data_b64:
            return _ok200()  # e.g. Pub/Sub subscription verification ping
        rtdn = json.loads(base64.b64decode(data_b64))
    except Exception:
        return _ok200()
    # 4. Sanity-check packageName.
    if rtdn.get("packageName") and rtdn["packageName"] != _settings.google_package_name:
        return _ok200()
    # 5. Run the sync handler off the event loop.
    await run_in_threadpool(_dispatch_rtdn, rtdn)
    return _ok200()


def _dispatch_rtdn(rtdn: dict) -> None:
    db = SessionLocal()
    try:
        if "subscriptionNotification" in rtdn:
            _handle_subscription_rtdn(db, rtdn["subscriptionNotification"], base_plan_ids(db))
        elif "voidedPurchaseNotification" in rtdn:
            _handle_voided_rtdn(db, rtdn["voidedPurchaseNotification"])
        elif "oneTimeProductNotification" in rtdn:
            pass  # safety-net log only; client /purchase/verify does the grant
        elif "testNotification" in rtdn:
            pass
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _handle_subscription_rtdn(db: Session, note: dict, base_plans: dict) -> None:
    ntype = note.get("notificationType")
    token = note.get("purchaseToken")
    if not token:
        return
    verified = pb.verify_subscription_purchase(token)
    real_tier = tier_for_base_plan(pb.get_base_plan_id(verified), base_plans)
    expiry = pb.get_expiry_epoch(verified)

    # PURCHASED handled BEFORE the "user exists" guard (brand-new token, no user row yet).
    if ntype == pb.SUB_PURCHASED:
        linked = pb.get_linked_purchase_token(verified)
        if linked:
            _revoke_linked_token(db, linked)
        return  # grant waits on the client call (RTDN carries no device/uid)

    purchase = db.get(Purchase, token)
    if purchase is None:
        return  # no ledger row yet; the client's lazy-refill will reconcile
    user = db.get(User, purchase.uid)
    if user is None:
        return
    sku = real_tier.sku if real_tier else purchase.sku

    if ntype in (pb.SUB_RENEWED, pb.SUB_RECOVERED, pb.SUB_RESTARTED):
        # Idempotency-skip if granted within 90% of the REAL period (duration, not a drip cycle).
        if real_tier and now_ms() - (purchase.granted_at or 0) < int(0.9 * duration_ms(real_tier)):
            return
        atomic_grant_sub(db, uid=user.uid, sku=sku, expiry_ms=expiry, purchase_token=token)
        atomic_bump_purchase_grant(db, token=token)
    elif ntype == pb.SUB_EXPIRED:
        # Drop entitlement WITHOUT moving expiry backward (Play may return no lineItems).
        if expiry is None or expiry <= now_ms():
            atomic_expire_sub(db, uid=user.uid)
    elif ntype == pb.SUB_REVOKED:
        atomic_revoke_purchase(db, token=token)
        set_premium_expired_now(db, uid=user.uid)
    elif ntype == pb.SUB_IN_GRACE:
        atomic_grant_sub(db, uid=user.uid, sku=sku, expiry_ms=expiry, purchase_token=token)
    elif ntype == pb.SUB_ON_HOLD:
        drop_premium_keep_sub(db, uid=user.uid)
    # CANCELED / PAUSED / DEFERRED / PRICE_CONFIRMED -> informational; sub stays until renew/expire.


def _handle_voided_rtdn(db: Session, note: dict) -> None:
    token = note.get("purchaseToken")
    if not token:
        return
    atomic_revoke_purchase(db, token=token)  # at-most-once clawback (WHERE revoked=0)
    purchase = db.get(Purchase, token)
    if purchase:
        user = db.get(User, purchase.uid)
        if user and user.purchase_token == token:
            set_premium_expired_now(db, uid=user.uid)
