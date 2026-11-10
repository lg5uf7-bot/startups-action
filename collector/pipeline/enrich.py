from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import psycopg

from .. import db
from ..ats.detect import detect_ats
from ..config import Settings
from ..http import HttpClient
from ..models import Candidate, CompanyRecord
from ..normalize import (
    clean_text,
    first_nonempty,
    safe_domain,
    strip_html,
    unique_strings,
)
from ..scoring import score_company
from ..sources.startupdb import StartupDB

log = logging.getLogger(__name__)


def _candidate_description(candidate: Candidate) -> str | None:
    raw = candidate.raw or {}
    values: list[Any] = []
    if isinstance(raw, dict):
        values.extend(
            [
                raw.get("description"),
                raw.get("oneLiner"),
                raw.get("shortDescription"),
                raw.get("text"),
            ]
        )
        record = raw.get("record")
        if isinstance(record, dict):
            values.extend(
                [
                    record.get("description"),
                    record.get("oneLiner"),
                    record.get("shortDescription"),
                    record.get("longDescription"),
                ]
            )
        job = raw.get("job")
        if isinstance(job, dict):
            values.extend([job.get("description"), job.get("one_liner")])
    for value in values:
        text = clean_text(value, max_chars=1000)
        if text and len(text) > 20:
            return text
    return None


def _candidate_tags(candidate: Candidate) -> list[str]:
    raw = candidate.raw or {}
    values: list[Any] = []
    if isinstance(raw, dict):
        values.extend([raw.get("tags"), raw.get("industries")])
        record = raw.get("record")
        if isinstance(record, dict):
            values.extend([record.get("tags"), record.get("industries"), record.get("markets")])
        job = raw.get("job")
        if isinstance(job, dict):
            values.append(job.get("tags"))
    tags: list[str] = []
    for value in values:
        tags.extend(unique_strings(value))
    return tags[:30]


def _candidate_logo(candidate: Candidate) -> str | None:
    raw = candidate.raw or {}
    if not isinstance(raw, dict):
        return None
    record = raw.get("record") if isinstance(raw.get("record"), dict) else {}
    job = raw.get("job") if isinstance(raw.get("job"), dict) else {}
    value = first_nonempty(
        raw.get("logo"),
        record.get("logo"),
        record.get("smallLogoUrl"),
        job.get("company_logo"),
        job.get("logo"),
    )
    return str(value).strip() if value else None


def _to_candidate(row: dict[str, Any]) -> Candidate:
    return Candidate(
        company_key=str(row.get("company_key") or ""),
        name=str(row.get("name") or "").strip(),
        domain=row.get("domain"),
        website_url=row.get("website_url"),
        ats_type=row.get("ats_type"),
        ats_slug=row.get("ats_slug"),
        discovery_source=str(row.get("discovery_source") or ""),
        discovery_url=row.get("discovery_url"),
        raw=row.get("raw_data") or {},
    )


def run_enrichment(
    conn: psycopg.Connection,
    client: HttpClient,
    settings: Settings,
    run: dict[str, Any],
    run_day: date,
    startupdb: StartupDB,
) -> dict[str, Any]:
    run_id = str(run["id"])
    rows = db.claim_candidates(conn, settings.max_enrich)
    company_ids: list[str] = []
    enriched = 0
    failed = 0
    reviews = 0

    for row in rows:
        started = time.monotonic()
        candidate = _to_candidate(row)
        candidate_id = str(row["id"])
        item_key = candidate.company_key or candidate_id
        try:
            enrichment = startupdb.enrich(candidate)
            website = candidate.website_url or enrichment.get("website_url")
            domain = candidate.domain or enrichment.get("domain") or safe_domain(website)
            name = (candidate.name or enrichment.get("name") or "").strip()
            description = enrichment.get("short_description") or _candidate_description(candidate)
            tags = _candidate_tags(candidate)
            industries = unique_strings(enrichment.get("industries") or tags)
            raw_sources = [
                {
                    "source": candidate.discovery_source or "discovery",
                    "record": candidate.raw,
                }
            ]
            if enrichment.get("raw"):
                raw_sources.append(
                    {
                        "source": "startupdb",
                        "record": enrichment["raw"],
                    }
                )

            company = CompanyRecord(
                name=name,
                domain=domain,
                website_url=website,
                short_description=description,
                founded_year=enrichment.get("founded_year"),
                headquarters=enrichment.get("headquarters"),
                country=None,
                city=None,
                linkedin_url=enrichment.get("linkedin_url"),
                x_url=enrichment.get("x_url"),
                logo_url=enrichment.get("logo_url") or _candidate_logo(candidate),
                careers_url=None,
                employee_count=None,
                industries=industries,
                tags=tags,
                last_funding_round=enrichment.get("last_funding_round"),
                last_funding_date=enrichment.get("last_funding_date"),
                funding_date_raw=enrichment.get("funding_date_raw"),
                funding_date_precision=enrichment.get("funding_date_precision"),
                last_funding_amount=enrichment.get("last_funding_amount"),
                last_funding_currency=enrichment.get("last_funding_currency"),
                total_funding_usd=enrichment.get("total_funding_usd"),
                discovery_source=candidate.discovery_source,
                discovery_url=candidate.discovery_url,
                source_synced_at=enrichment.get("source_synced_at"),
                raw_sources=raw_sources,
            )

            if not company.name:
                db.mark_candidate(conn, candidate_id, "rejected", error="missing_name")
                db.add_review(
                    conn,
                    run_id=run_id,
                    run_day=run_day,
                    company_id=None,
                    name=candidate.name or "(unknown)",
                    domain=domain,
                    website=website,
                    reason_code="missing_name",
                    details="Candidate had no usable company name.",
                    raw=candidate.raw,
                )
                failed += 1
                reviews += 1
                db.record_item(
                    conn,
                    run_id,
                    "enrichment",
                    item_key,
                    "failed",
                    error_code="missing_name",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                conn.commit()
                continue

            if not website and not candidate.ats_type:
                db.mark_candidate(conn, candidate_id, "rejected", error="no_website_or_ats")
                db.add_review(
                    conn,
                    run_id=run_id,
                    run_day=run_day,
                    company_id=None,
                    name=company.name,
                    domain=domain,
                    website=None,
                    reason_code="no_website_or_ats",
                    details="No company website and no ATS board could be identified.",
                    raw=candidate.raw,
                )
                failed += 1
                reviews += 1
                db.record_item(
                    conn,
                    run_id,
                    "enrichment",
                    item_key,
                    "failed",
                    error_code="no_website_or_ats",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                conn.commit()
                continue

            board = detect_ats(client, candidate)
            score = score_company(company, board)
            if score < settings.min_quality_score:
                db.mark_candidate(conn, candidate_id, "rejected", error="low_quality")
                db.add_review(
                    conn,
                    run_id=run_id,
                    run_day=run_day,
                    company_id=None,
                    name=company.name,
                    domain=domain,
                    website=website,
                    reason_code="low_quality",
                    details=f"Quality score {score} is below {settings.min_quality_score}.",
                    raw={"candidate": candidate.raw, "score": score},
                )
                failed += 1
                reviews += 1
                db.record_item(
                    conn,
                    run_id,
                    "enrichment",
                    item_key,
                    "failed",
                    error_code="low_quality",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    details={"score": score},
                )
                conn.commit()
                continue

            company_id = db.upsert_company(
                conn,
                company,
                board=board,
                quality_score=score,
                min_quality_score=settings.min_quality_score,
            )
            company_ids.append(company_id)
            db.mark_candidate(conn, candidate_id, "enriched")
            enriched += 1
            db.record_item(
                conn,
                run_id,
                "enrichment",
                item_key,
                "ok",
                duration_ms=int((time.monotonic() - started) * 1000),
                details={
                    "company_id": company_id,
                    "score": score,
                    "ats": f"{board.provider}/{board.token}" if board else None,
                },
            )
            conn.commit()
            log.info(
                "enriched %s -> %s (score=%s, ats=%s)",
                company.name,
                company_id,
                score,
                f"{board.provider}/{board.token}" if board else "none",
            )
        except Exception as exc:
            conn.rollback()
            failed += 1
            try:
                db.mark_candidate(conn, candidate_id, "retry", error=str(exc)[:300])
                db.record_item(
                    conn,
                    run_id,
                    "enrichment",
                    item_key,
                    "retry",
                    error_code="enrichment_error",
                    duration_ms=int((time.monotonic() - started) * 1000),
                    details={"error": str(exc)[:300]},
                )
                conn.commit()
            except Exception:
                conn.rollback()
            log.exception("enrichment failed for %s", candidate.name)

    return {
        "processed": len(rows),
        "enriched": enriched,
        "failed": failed,
        "reviews": reviews,
        "company_ids": company_ids,
    }