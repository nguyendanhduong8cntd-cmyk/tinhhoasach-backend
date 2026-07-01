"""Google Play verification + Pub/Sub RTDN JWT verification (playbook §3, §5).

Two modes chosen by env:
  * DEV  (no GOOGLE_SERVICE_ACCOUNT_JSON) -> every verify_* returns a stub that
    passes, so you can run the Play License-Testing flow before the app is on Play.
  * PROD (service account set) -> real Google Play Developer API calls.
"""
from __future__ import annotations

import datetime
import json
import time
from typing import Optional

from .config import get_settings

_settings = get_settings()

# RTDN subscription notificationType codes
SUB_RECOVERED = 1
SUB_RENEWED = 2
SUB_CANCELED = 3
SUB_PURCHASED = 4
SUB_ON_HOLD = 5
SUB_IN_GRACE = 6
SUB_RESTARTED = 7
SUB_PRICE_CONFIRMED = 8
SUB_DEFERRED = 9
SUB_PAUSED = 10
SUB_PAUSE_SCHEDULE_CHANGED = 11
SUB_REVOKED = 12
SUB_EXPIRED = 13


def prod_enabled() -> bool:
    return bool(_settings.google_service_account_json)


# ── real client (lazy; only imported in PROD) ───────────────────────
def _load_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    raw = _settings.google_service_account_json
    scopes = ["https://www.googleapis.com/auth/androidpublisher"]
    if raw and raw.strip().startswith("{"):
        creds = service_account.Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    else:
        creds = service_account.Credentials.from_service_account_file(raw, scopes=scopes)
    return build("androidpublisher", "v3", credentials=creds, cache_discovery=False)


# ── verification ────────────────────────────────────────────────────
def verify_subscription_purchase(token: str, *, hint_base_plan: Optional[str] = None,
                                 hint_duration_days: int = 7) -> dict:
    """subscriptionsv2.get (PROD) or a plausible ACTIVE stub (DEV)."""
    if not prod_enabled():
        expiry = now_ms() + hint_duration_days * 86_400_000
        return {
            "stub": True,
            "subscriptionState": "SUBSCRIPTION_STATE_ACTIVE",
            "linkedPurchaseToken": None,
            "lineItems": [{
                "expiryTime": _to_iso(expiry),
                "offerDetails": {"basePlanId": hint_base_plan or "release-weekly-plan"},
            }],
        }
    return _load_service().purchases().subscriptionsv2().get(
        packageName=_settings.google_package_name, token=token).execute()


def verify_one_time_purchase(token: str, product_id: str) -> dict:
    """products.get (PROD) or a PURCHASED stub (DEV)."""
    if not prod_enabled():
        return {"stub": True, "purchaseState": 0, "productId": product_id}
    return _load_service().purchases().products().get(
        packageName=_settings.google_package_name, productId=product_id, token=token).execute()


def is_purchase_state_purchased(verified: dict) -> bool:
    if "subscriptionState" in verified:
        active = verified.get("subscriptionState") in (
            "SUBSCRIPTION_STATE_ACTIVE", "SUBSCRIPTION_STATE_IN_GRACE_PERIOD")
        exp = get_expiry_epoch(verified)
        return active and (exp is None or exp > now_ms())
    if "purchaseState" in verified:
        return verified.get("purchaseState") == 0  # 0=PURCHASED (not 2=PENDING)
    return False


def get_base_plan_id(verified: dict) -> Optional[str]:
    for li in verified.get("lineItems") or []:
        base = (li.get("offerDetails") or {}).get("basePlanId")
        if base:
            return base
    return None


def get_expiry_epoch(verified: dict) -> Optional[int]:
    """Real expiry (use this, not server now(), so clock skew can't cut VIP early)."""
    best: Optional[int] = None
    for li in verified.get("lineItems") or []:
        t = li.get("expiryTime")
        if t:
            ms = _parse_iso_ms(t)
            best = ms if best is None else max(best, ms)
    return best


def get_linked_purchase_token(verified: dict) -> Optional[str]:
    return verified.get("linkedPurchaseToken")


def get_subscription_state(verified: dict) -> Optional[str]:
    return verified.get("subscriptionState")


def acknowledge_subscription(token: str) -> None:
    """MUST run within Google's 3-day window or the purchase auto-refunds."""
    if not prod_enabled():
        return
    try:
        _load_service().purchases().subscriptions().acknowledge(
            packageName=_settings.google_package_name,
            subscriptionId=_settings.premium_product_id, token=token, body={}).execute()
    except Exception:
        pass  # best-effort; already-acked raises and is fine


def acknowledge_one_time(token: str, product_id: str) -> None:
    if not prod_enabled():
        return
    try:
        _load_service().purchases().products().acknowledge(
            packageName=_settings.google_package_name,
            productId=product_id, token=token, body={}).execute()
    except Exception:
        pass


# ── Pub/Sub OIDC JWT (fail CLOSED) ──────────────────────────────────
def verify_pubsub_jwt(authorization: Optional[str]) -> bool:
    """Verify the push subscription's bearer token. Returns True only when trusted.

    Fail closed: audience unset in prod => reject everything (else anyone can forge a
    RENEWED for free Pro, or a REVOKED to wipe a victim). Local escape hatch is
    ALLOW_UNSIGNED_RTDN, which assert_prod_ready() refuses to boot with in prod.
    """
    s = _settings
    if not s.rtdn_audience_set:
        return bool(s.allow_unsigned_rtdn)  # unset audience: only local escape hatch passes
    if not authorization or not authorization.lower().startswith("bearer "):
        return False
    token = authorization.split(" ", 1)[1].strip()
    try:
        import jwt
        from jwt import PyJWKClient

        signing_key = PyJWKClient("https://www.googleapis.com/oauth2/v3/certs").get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, signing_key.key, algorithms=["RS256"],
            audience=s.google_pubsub_audience, options={"require": ["exp", "iss", "aud"]})
        return claims.get("iss") in ("https://accounts.google.com", "accounts.google.com")
    except Exception:
        return False


# ── time helpers ────────────────────────────────────────────────────
def now_ms() -> int:
    return int(time.time() * 1000)


def _to_iso(ms: int) -> str:
    return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _parse_iso_ms(s: str) -> int:
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return now_ms()
