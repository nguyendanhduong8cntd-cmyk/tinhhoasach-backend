"""Server-driven remote config (spec §2). Defaults live in code; any top-level key
can be overridden by a row in the ``remote_config`` table without an app update.

The SKU convention lives here: ``base_plans`` maps each bucket to one of the three
default base-plan ids (``release-*-plan``). The client reads this, maps a bucket to a
base plan, and buys ``premium_product_id`` with that plan; the server verifies the
real base plan back out.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import RemoteConfig


def default_config() -> dict:
    s = get_settings()
    return {
        "min_version": "1.0.0",
        "in_maintenance": False,
        "display_mode": {"first_open": "light"},
        "onboarding_flow": {"category_selection": 2},
        "gift_flags": {
            "pro_button": {"is_active": True},
            "floating_gift_button": {"is_active": False},
            "special_offer_after_default_paywall": {"is_active": False},
        },
        "paywall_modifiers": {
            "button_glow_animation": True,
            "try_for_free_screen": True,
        },
        # ── billing wiring the client needs ──────────────────────────
        "premium_product_id": s.premium_product_id,
        "base_plans": {                       # ★ SKU convention, remote-config driven
            "1week": s.base_plan_weekly,
            "1month": s.base_plan_monthly,
            "1year": s.base_plan_yearly,
        },
        "iap_catalog": {                      # bucket -> tier -> Play product id
            "1week":      {"t1": {"product_id": "ths.pro.1week.t1"}, "t2": {"product_id": "ths.pro.1week.t2"}},
            "1weekFree":  {"t1": {"product_id": "ths.pro.freetrial3d.1week.t1"}},
            "1month":     {"t1": {"product_id": "ths.pro.1month.t1"}, "t2": {"product_id": "ths.pro.1month.t2"}},
            "1year":      {"t1": {"product_id": "ths.pro.1year.t1"}, "t2": {"product_id": "ths.pro.1year.t2"}},
            "lifetime":   {"t1": {"product_id": "ths.pro.lifetime.t1"}},
            "specialOffer1": {"t1": {"product_id": "ths.pro.1year.offer1"}},
        },
    }


def get_config(db: Session) -> dict:
    """Merge code defaults with DB overrides (shallow, per top-level key)."""
    cfg = default_config()
    for row in db.execute(select(RemoteConfig)).scalars():
        cfg[row.key] = row.value
    return cfg


def base_plan_ids(db: Session) -> dict:
    """The bucket->base-plan map used by billing to resolve the real tier."""
    return get_config(db).get("base_plans", {})
