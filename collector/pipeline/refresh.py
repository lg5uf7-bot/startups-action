from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import psycopg

from .. import db
from ..config import Settings
from ..http import HttpClient
from .jobs import sync_company_jobs

log = logging.getLogger(__name__)


def run_refresh(
    conn: psycopg.Connection,
    client: HttpClient,
    settings: Settings,
    run: dict[str, Any],
    run_day: date,
    *,
    deadline: float | None = None,
) -> dict[str, Any]:
    run_id = str(run["id"])
    companies = db.get_refresh_companies(conn, settings.max_refresh)
    succeeded = 0
    failed = 0
    jobs_synced = 0
    jobs_new = 0
    processed = 0

    for company in companies:
        if deadline is not None and time.monotonic() >= deadline:
            log.warning("refresh stopped at runtime budget")
            break
        processed += 1
        started = time.monotonic()
        try:
            outcome = sync_company_jobs(conn, client, company)
            duration = int((time.monotonic() - started) * 1000)
            if outcome.get("ok"):
                succeeded += 1 if int(outcome.get("active") or 0) > 0 else 0
                jobs_synced += int(outcome.get("jobs") or 0)
                jobs_new += int(outcome.get("new") or 0)
                db.record_item(
                    conn,
                    run_id,
                    "refresh",
                    str(company.get("slug") or company.get("id")),
                    "ok",
                    duration_ms=duration,
                    details=outcome,
                )
            else:
                failed += 1
                db.record_item(
                    conn,
                    run_id,
                    "refresh",
                    str(company.get("slug") or company.get("id")),
                    "retry",
                    http_status=outcome.get("http_status"),
                    error_code=str(outcome.get("error_code") or "fetch_failed"),
                    duration_ms=duration,
                    details=outcome,
                )
            conn.commit()
        except Exception as exc:
            conn.rollback()
            failed += 1
            try:
                db.record_item(
                    conn,
                    run_id,
                    "refresh",
                    str(company.get("slug") or company.get("id")),
                    "retry",
                    error_code="refresh_error",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    details={"error": str(exc)[:300]},
                )
                conn.commit()
            except Exception:
                conn.rollback()
            log.exception("refresh failed for %s", company.get("name"))

    return {
        "processed": processed,
        "succeeded": succeeded,
        "failed": failed,
        "jobs_synced": jobs_synced,
        "jobs_new": jobs_new,
    }