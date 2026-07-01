"""App settings + the prod-readiness gate (playbook §7.6 / spec §10)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # Core
    app_api_key: str = "dev-static-key-change-me"
    database_url: str = "sqlite:///./tinhhoasach.db"

    # Firebase (content rules only; v1 does not verify idToken server-side)
    firebase_project_id: str = "tinhhoasach"
    firebase_service_account_json: str | None = None

    # Google Play billing
    google_package_name: str = "io.tinhhoasach.app"
    premium_product_id: str = "ths.pro.sub"
    google_service_account_json: str | None = None
    google_pubsub_audience: str | None = None

    # SKU convention: the 3 default base plans (remote-config seed values)
    base_plan_weekly: str = "release-weekly-plan"
    base_plan_monthly: str = "release-monthly-plan"
    base_plan_yearly: str = "release-yearly-plan"

    # Safety gates
    require_prod: bool = False
    allow_unsigned_rtdn: bool = True

    @property
    def prod_verify_enabled(self) -> bool:
        """True when a real Play service account is configured (stub off)."""
        return bool(self.google_service_account_json)

    @property
    def rtdn_audience_set(self) -> bool:
        return bool(self.google_pubsub_audience)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def assert_prod_ready(settings: Settings) -> None:
    """Die at startup on insecure prod config instead of silently serving free VIP."""
    if not settings.require_prod:
        return
    problems: list[str] = []
    if not settings.prod_verify_enabled:
        problems.append("GOOGLE_SERVICE_ACCOUNT_JSON missing -> Play verify is STUB")
    if not settings.rtdn_audience_set:
        problems.append("GOOGLE_PUBSUB_AUDIENCE missing -> RTDN JWT cannot be verified")
    if settings.allow_unsigned_rtdn:
        problems.append("ALLOW_UNSIGNED_RTDN=1 is forbidden when REQUIRE_PROD=1")
    if settings.app_api_key in ("", "dev-static-key-change-me"):
        problems.append("APP_API_KEY is unset or still the default")
    if problems:
        raise RuntimeError(
            "REQUIRE_PROD=1 but config is insecure:\n  - " + "\n  - ".join(problems)
        )
