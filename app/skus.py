"""SKU model — ONE subscription product, 3 base plans (spec §8 / playbook §1).

Convention (per handoff note): the base-plan ids are ALWAYS the three defaults
``release-weekly-plan`` / ``release-monthly-plan`` / ``release-yearly-plan`` and are
served to the client from remote config, so pricing/plan wiring can change without
an app update. ``sub_for`` resolves either the internal tier id, the bucket, the
Google base-plan id, or a catalog product id — old and new clients both work.
"""
from __future__ import annotations

from dataclasses import dataclass

DAY_MS = 86_400_000


@dataclass(frozen=True)
class SubTier:
    sku: str            # internal tier id (stable, stored in DB)
    bucket: str         # remote-config bucket key: 1week / 1month / 1year
    duration_days: int  # REAL period — use for renewal idempotency, never a drip cycle
    rank: int           # highest-active wins on restore (yearly > monthly > weekly)


WEEKLY = SubTier("weekly_pro", "1week", 7, 1)
MONTHLY = SubTier("monthly_pro", "1month", 30, 2)
YEARLY = SubTier("yearly_pro", "1year", 365, 3)
LIFETIME_SKU = "lifetime_pro"

_BY_BUCKET = {t.bucket: t for t in (WEEKLY, MONTHLY, YEARLY)}
_BY_SKU = {t.sku: t for t in (WEEKLY, MONTHLY, YEARLY)}


def tier_for_base_plan(base_plan_id: str | None, base_plans: dict) -> SubTier | None:
    """Map the REAL Google base-plan id back to an internal tier (anti-spoof, §7.1).

    ``base_plans`` is the remote-config bucket->plan map, e.g.
    ``{"1week": "release-weekly-plan", "1month": "release-monthly-plan", ...}``.
    """
    if not base_plan_id:
        return None
    for bucket, plan in base_plans.items():
        if plan == base_plan_id and bucket in _BY_BUCKET:
            return _BY_BUCKET[bucket]
    return None


def sub_for(x: str | None, base_plans: dict) -> SubTier | None:
    """Resolve ANY of: internal tier id | bucket | google base-plan id | catalog product id."""
    if not x:
        return None
    if x in _BY_SKU:
        return _BY_SKU[x]
    if x in _BY_BUCKET:
        return _BY_BUCKET[x]
    by_plan = tier_for_base_plan(x, base_plans)
    if by_plan:
        return by_plan
    low = x.lower()  # last resort: infer from a catalog product id like ths.pro.1year.t1
    if "year" in low:
        return YEARLY
    if "month" in low:
        return MONTHLY
    if "week" in low:
        return WEEKLY
    return None


def duration_ms(tier: SubTier) -> int:
    return tier.duration_days * DAY_MS


def is_lifetime(product_id: str | None) -> bool:
    return bool(product_id) and "lifetime" in product_id.lower()
