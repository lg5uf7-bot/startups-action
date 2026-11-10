from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import psycopg

from .. import db
from ..ats.adapters import fetch_jobs
from ..ats.patterns import board_url
from ..config import Settings
from ..http import HttpClient
from ..models import AtsBoard

log = logging.getLogger(__name__)


def _board_from_company(company: dict[str, Any]) -> AtsBoard | None:
    provider = str(company.get("ats_type") or "").strip().lower()
    token = str(company.get("ats_slug") or "").strip()
    if not provider or not token:
        return None
    region = "global"
    sources = company.get("ats_sources") or []
    if isinstance(sources, list):
        for source in sources:
            if (
                isinstance(source, dict)
                and source.get("provider") == provider
                and str(source.get("token")) == token
            ):
                region = str(source.get("region") or "global")
                break
    if "eu.lever.co" in str(company.get("ats_board_url") or ""):
        region = "eu"
    page, api = board_url(provider, token, region)
    return AtsBoard(
        provider=provider,
        token=token,
        board_url=str(company.get("ats_board_url") or page),
        api_url=api,
        confidence=float(company.get("ats_confidence") or 1.0),
        detected_via=str(company.get("ats_detected_via") or "stored"),
        region=region,
    )


def sync_company_jobs(
    conn: psycopg.Connection,
    client: HttpClient,
    company: dict[str, Any],
) -> dict[str, Any]:
    company_id = str(company["id"])
    board = _board_from_company(company)
    if not board:
        return {"ok": False, "error_code": "no_ats", "jobs": 0, "new": 0, "changed": 0}
    result = fetch_jobs(client, board)
    if not result.ok:
        return {
            "ok": False,
            "error_code": result.error_code or "fetch_failed",
            "http_status": result.http_status,
            "error": result.error,
            "jobs": 0,
            "new": 0,
            "changed": 0,
        }

    source_keys: list[str] = []
    new_jobs = 0
    changed_jobs = 0
    for job in result.jobs:
        _, inserted, changed = db.upsert_job(
            conn,
            company_id,
            board,
            job,
            today=date.today(),
        )
        source_keys.append(f"{board.provider}|{board.region}|{board.token}|{job.external_id}")
        new_jobs += 1 if inserted else 0
        changed_jobs += 1 if changed else 0
    db.deactivate_missing(conn, company_id, board, source_keys)
    stats = db.update_company_job_stats(conn, company_id)
    conn.commit()
    return {
        "ok": True,
        "jobs": len(result.jobs),
        "new": new_jobs,
        "changed": changed_jobs,
        "active": stats["active"],
        "total": stats["total"],
        "board": f"{board.provider}/{board.token}",
    }


def run_jobs(
    conn: psycopg.Connection,
    client: HttpClient,
    settings: Settings,
    run: dict[str, Any],
    run_day: date,
    company_ids: list[str],
) -> dict[str, Any]:
    run_id = str(run["id"])
    seen: set[str] = set()
    succeeded = 0
    failed = 0
    jobs_synced = 0
    jobs_new = 0

    for company_id in company_ids:
        if company_id in seen:
            continue
        seen.add(company_id)
        if len(seen) > settings.max_new_jobs:
            break
        started = time.monotonic()
        company = db.get_company(conn, company_id)
        if not company:
            continue
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
                    "jobs",
                    str(company.get("slug") or company_id),
                    "ok",
                    duration_ms=duration,
                    details=outcome,
                )
            else:
                failed += 1
                db.record_item(
                    conn,
                    run_id,
                    "jobs",
                    str(company.get("slug") or company_id),
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
                    "jobs",
                    str(company.get("slug") or company_id),
                    "retry",
                    error_code="job_sync_error",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    details={"error": str(exc)[:300]},
                )
                conn.commit()
            except Exception:
                conn.rollback()
            log.exception("job sync failed for %s", company.get("name"))

    return {
        "companies": len(seen),
        "succeeded": succeeded,
        "failed": failed,
        "jobs_synced": jobs_synced,
        "jobs_new": jobs_new,
    }