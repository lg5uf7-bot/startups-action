"""Runtime configuration.

All values come from environment variables so the same code runs locally and
inside GitHub Actions. The existing repository secret is SIM_DATABASE_URL; the
other names are accepted as fallbacks for local use.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    telegram_bot_token: str
    telegram_chat_id: str
    alert_webhook_url: str
    dry_run: bool
    max_candidates: int
    max_enrich: int
    max_new_jobs: int
    max_refresh: int
    max_runtime_seconds: int
    http_delay: float
    startupdb_delay: float
    user_agent: str
    min_quality_score: int
    log_level: str


def load_settings() -> Settings:
    database_url = (
        os.getenv("SIM_DATABASE_URL")
        or os.getenv("SUPABASE_DB_URL")
        or os.getenv("DATABASE_URL")
        or ""
    )
    return Settings(
        database_url=database_url,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        # Kept as a fallback for the existing workflow. Move it to a secret when
        # the repository is public.
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or "8420407013",
        alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL", ""),
        dry_run=_bool("DRY_RUN"),
        max_candidates=_int("MAX_CANDIDATES", 80),
        max_enrich=_int("MAX_ENRICH", 25),
        max_new_jobs=_int("MAX_NEW_JOBS", 25),
        max_refresh=_int("MAX_REFRESH", 120),
        max_runtime_seconds=_int("MAX_RUNTIME_SECONDS", 2700),
        http_delay=_float("HTTP_DELAY", 1.0),
        startupdb_delay=_float("STARTUPDB_DELAY", 0.55),
        user_agent=os.getenv(
            "USER_AGENT",
            "StartupCollector/2.0 (+https://github.com/lg5uf7-bot/startups-action)",
        ),
        min_quality_score=_int("MIN_QUALITY_SCORE", 30),
        log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
    )