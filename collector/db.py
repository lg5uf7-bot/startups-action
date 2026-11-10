from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Any, Iterable

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .config import Settings
from .models import AtsBoard, Candidate, CompanyRecord, JobRecord
from .normalize import json_hash, name_key, slugify

log = logging.getLogger(__name__)

LOCK_KEY = 461933052617


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def connect(settings: Settings) -> psycopg.Connection:
    if not settings.database_url:
        raise RuntimeError(
            "Missing database URL. Set SIM_DATABASE_URL (or SUPABASE_DB_URL / DATABASE_URL)."
        )
    return psycopg.connect(
        settings.database_url,
        autocommit=False,
        row_factory=dict_row,
    )


def try_lock(conn: psycopg.Connection) -> bool:
    row = conn.execute(
        "select pg_try_advisory_lock(%s) as locked", (LOCK_KEY,)
    ).fetchone()
    conn.commit()
    return bool(row and row["locked"])


def unlock(conn: psycopg.Connection) -> None:
    try:
        conn.execute("select pg_advisory_unlock(%s)", (LOCK_KEY,))
        conn.commit()
    except Exception:  # pragma: no cover - best effort
        conn.rollback()


def _github_context() -> dict[str, Any]:
    run_id = os.getenv("GITHUB_RUN_ID") or ""
    server = os.getenv("GITHUB_SERVER_URL") or ""
    repository = os.getenv("GITHUB_REPOSITORY") or ""
    context = {
        "run_id": run_id or None,
        "run_number": os.getenv("GITHUB_RUN_NUMBER") or None,
        "workflow": os.getenv("GITHUB_WORKFLOW") or None,
        "job": os.getenv("GITHUB_JOB") or None,
        "actor": os.getenv("GITHUB_ACTOR") or None,
        "repository": repository or None,
        "ref": os.getenv("GITHUB_REF_NAME") or None,
        "sha": os.getenv("GITHUB_SHA") or None,
        "event": os.getenv("GITHUB_EVENT_NAME") or None,
        "server_url": server or None,
    }
    if run_id and server and repository:
        context["run_url"] = f"{server}/{repository}/actions/runs/{run_id}"
    return {key: value for key, value in context.items() if value}


def start_run(
    conn: psycopg.Connection,
    run_key: str,
    run_date: date,
    mode: str,
) -> dict[str, Any]:
    row = conn.execute(
        """
        insert into public.runs
            (run_key, run_date, mode, status, started_at, github, metadata)
        values (%s, %s, %s, 'running', now(), %s, %s)
        on conflict (run_key) do update set
            status = 'running',
            started_at = now(),
            finished_at = null,
            error = null,
            discovered = 0,
            enriched = 0,
            succeeded = 0,
            failed = 0,
            jobs_synced = 0,
            reviews = 0,
            phase_timings = '{}'::jsonb,
            items = '[]'::jsonb,
            logs = '[]'::jsonb,
            github = excluded.github,
            metadata = excluded.metadata,
            updated_at = now()
        returning *
        """,
        (
            run_key,
            run_date,
            mode,
            Jsonb(_github_context()),
            Jsonb({"run_key": run_key, "mode": mode}),
        ),
    ).fetchone()
    conn.commit()
    return row


def finish_run(
    conn: psycopg.Connection,
    run_id: str,
    *,
    status: str,
    discovered: int = 0,
    enriched: int = 0,
    succeeded: int = 0,
    failed: int = 0,
    jobs_synced: int = 0,
    reviews: int = 0,
    phase_timings: dict[str, Any] | None = None,
    error: str | None = None,
    notes: str | None = None,
) -> None:
    conn.execute(
        """
        update public.runs set
            status = %s,
            finished_at = now(),
            discovered = %s,
            enriched = %s,
            succeeded = %s,
            failed = %s,
            jobs_synced = %s,
            reviews = %s,
            phase_timings = %s,
            error = %s,
            notes = %s,
            updated_at = now()
        where id = %s
        """,
        (
            status,
            discovered,
            enriched,
            succeeded,
            failed,
            jobs_synced,
            reviews,
            Jsonb(phase_timings or {}),
            error,
            notes,
            run_id,
        ),
    )
    conn.commit()


def update_run_metadata(
    conn: psycopg.Connection,
    run_id: str,
    value: dict[str, Any],
) -> None:
    conn.execute(
        """
        update public.runs
        set metadata = coalesce(metadata, '{}'::jsonb) || %s::jsonb,
            updated_at = now()
        where id = %s
        """,
        (Jsonb(value), run_id),
    )
    conn.commit()


def record_item(
    conn: psycopg.Connection,
    run_id: str,
    phase: str,
    item_key: str,
    status: str,
    *,
    http_status: int | None = None,
    error_code: str | None = None,
    duration_ms: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    item = {
        "phase": phase,
        "item_key": item_key,
        "status": status,
        "http_status": http_status,
        "error_code": error_code,
        "duration_ms": duration_ms,
        "details": details or {},
        "at": utcnow().isoformat(),
    }
    conn.execute(
        """
        update public.runs
        set items = coalesce(items, '[]'::jsonb) || %s::jsonb,
            updated_at = now()
        where id = %s
        """,
        (Jsonb([item]), run_id),
    )


def reset_stuck_candidates(conn: psycopg.Connection) -> None:
    conn.execute(
        """
        update public.candidates
        set status = 'retry',
            last_error = 'stuck processing reset',
            updated_at = now()
        where status = 'processing'
          and updated_at < now() - interval '2 hours'
        """
    )
    conn.commit()


def upsert_candidate(conn: psycopg.Connection, candidate: Candidate) -> dict[str, Any]:
    row = conn.execute(
        """
        insert into public.candidates
            (company_key, name, domain, website_url, ats_type, ats_slug,
             discovery_source, discovery_url, discovery_sources, raw_data)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        on conflict (company_key) do update set
            domain = coalesce(candidates.domain, excluded.domain),
            website_url = coalesce(candidates.website_url, excluded.website_url),
            ats_type = coalesce(candidates.ats_type, excluded.ats_type),
            ats_slug = coalesce(candidates.ats_slug, excluded.ats_slug),
            discovery_url = coalesce(candidates.discovery_url, excluded.discovery_url),
            discovery_sources = coalesce((
                select array_agg(distinct value)
                from unnest(candidates.discovery_sources || excluded.discovery_sources) as value
            ), '{}'),
            raw_data = candidates.raw_data || excluded.raw_data,
            updated_at = now()
        returning *
        """,
        (
            candidate.company_key,
            candidate.name,
            candidate.domain,
            candidate.website_url,
            candidate.ats_type,
            candidate.ats_slug,
            candidate.discovery_source,
            candidate.discovery_url,
            [candidate.discovery_source] if candidate.discovery_source else [],
            Jsonb(candidate.raw or {}),
        ),
    ).fetchone()
    return row


def claim_candidates(conn: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        with picked as (
            select id
            from public.candidates
            where status in ('new', 'retry')
              and attempts < 5
            order by discovered_at asc
            limit %s
            for update skip locked
        )
        update public.candidates c
        set status = 'processing',
            attempts = c.attempts + 1,
            updated_at = now()
        from picked
        where c.id = picked.id
        returning c.*
        """,
        (limit,),
    ).fetchall()
    conn.commit()
    return rows


def mark_candidate(
    conn: psycopg.Connection,
    candidate_id: str,
    status: str,
    *,
    error: str | None = None,
) -> None:
    conn.execute(
        """
        update public.candidates
        set status = %s,
            last_error = %s,
            processed_at = now(),
            updated_at = now()
        where id = %s
        """,
        (status, error, candidate_id),
    )


def find_company(
    conn: psycopg.Connection,
    *,
    domain: str | None,
    name: str,
    website: str | None = None,
) -> dict[str, Any] | None:
    if domain:
        row = conn.execute(
            "select * from public.companies where lower(domain) = lower(%s) limit 1",
            (domain,),
        ).fetchone()
        if row:
            return row
    normalized = name_key(name)
    if not normalized:
        return None
    if website:
        row = conn.execute(
            """
            select * from public.companies
            where normalized_name = %s
              and (website is null or website = %s)
            order by created_at
            limit 1
            """,
            (normalized, website),
        ).fetchone()
    else:
        row = conn.execute(
            """
            select * from public.companies
            where normalized_name = %s
            order by created_at
            limit 1
            """,
            (normalized,),
        ).fetchone()
    return row


def _merge_company_data(
    existing: list[Any] | None,
    raw_sources: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = [item for item in (existing or []) if isinstance(item, dict)]
    seen = {item.get("hash") for item in merged if item.get("hash")}
    for source in raw_sources:
        if not isinstance(source, dict):
            continue
        record = source.get("record")
        digest = source.get("hash") or json_hash(record)
        if digest in seen:
            continue
        merged.append(
            {
                "source": source.get("source"),
                "fetched_at": source.get("fetched_at") or utcnow().isoformat(),
                "hash": digest,
                "record": record,
            }
        )
        seen.add(digest)
    return merged[-40:]


def _merge_ats_sources(
    existing: list[Any] | None,
    board: AtsBoard | None,
) -> list[dict[str, Any]]:
    merged = [item for item in (existing or []) if isinstance(item, dict)]
    if not board:
        return merged
    now = utcnow().isoformat()
    for item in merged:
        if (
            item.get("provider") == board.provider
            and item.get("token") == board.token
            and item.get("region", "global") == board.region
        ):
            item.update(
                {
                    "board_url": board.board_url,
                    "api_url": board.api_url,
                    "status": "verified",
                    "confidence": board.confidence,
                    "detected_via": board.detected_via,
                    "name": board.name,
                    "last_probed_at": now,
                }
            )
            return merged
    merged.append(
        {
            "provider": board.provider,
            "token": board.token,
            "region": board.region,
            "board_url": board.board_url,
            "api_url": board.api_url,
            "status": "verified",
            "confidence": board.confidence,
            "detected_via": board.detected_via,
            "name": board.name,
            "last_probed_at": now,
        }
    )
    return merged


def _unique_slug(
    conn: psycopg.Connection,
    *,
    desired: str,
    domain: str | None,
    name: str,
    company_id: str | None = None,
) -> str:
    base = slugify(desired or name)
    candidate = base
    for _ in range(5):
        row = conn.execute(
            "select id from public.companies where slug = %s limit 1",
            (candidate,),
        ).fetchone()
        if not row or (company_id and str(row["id"]) == str(company_id)):
            return candidate
        suffix = json_hash(domain or name)[:6]
        candidate = f"{base}-{suffix}"
        base = f"{base}-x"
    return f"{base}-{json_hash(domain or name)[:10]}"


def upsert_company(
    conn: psycopg.Connection,
    company: CompanyRecord,
    *,
    board: AtsBoard | None,
    quality_score: int,
    min_quality_score: int,
) -> str:
    existing = find_company(
        conn,
        domain=company.domain,
        name=company.name,
        website=company.website_url,
    )
    company_data = _merge_company_data(
        existing.get("company_data") if existing else None,
        company.raw_sources,
    )
    ats_sources = _merge_ats_sources(
        existing.get("ats_sources") if existing else None,
        board,
    )

    content_payload = {
        "name": company.name,
        "domain": company.domain,
        "website": company.website_url,
        "short_description": company.short_description,
        "founded_year": company.founded_year,
        "headquarters": company.headquarters,
        "logo_url": company.logo_url,
        "ats_type": board.provider if board else (existing or {}).get("ats_type"),
        "ats_slug": board.token if board else (existing or {}).get("ats_slug"),
        "last_funding_round": company.last_funding_round,
        "last_funding_date": company.last_funding_date,
    }
    new_hash = json_hash(content_payload)
    changed = not existing or existing.get("content_hash") != new_hash

    if board:
        ats_status = "verified"
    elif existing and existing.get("ats_status") in {"verified", "review"}:
        ats_status = existing["ats_status"]
    else:
        ats_status = "no_ats_found"

    is_public = quality_score >= min_quality_score
    enrichment_status = (
        "full"
        if company.short_description and (company.founded_year or company.last_funding_round)
        else "partial"
    )

    if existing:
        company_id = str(existing["id"])
        slug = existing.get("slug") or _unique_slug(
            conn,
            desired=company.name,
            domain=company.domain,
            name=company.name,
            company_id=company_id,
        )
        conn.execute(
            """
            update public.companies set
                name = coalesce(nullif(%s, ''), name),
                normalized_name = %s,
                slug = %s,
                website = coalesce(nullif(%s, ''), website),
                domain = coalesce(nullif(%s, ''), domain),
                short_description = coalesce(nullif(%s, ''), short_description),
                founded_year = coalesce(%s, founded_year),
                headquarters = coalesce(nullif(%s, ''), headquarters),
                country = coalesce(nullif(%s, ''), country),
                city = coalesce(nullif(%s, ''), city),
                linkedin_url = coalesce(nullif(%s, ''), linkedin_url),
                x_url = coalesce(nullif(%s, ''), x_url),
                logo_url = coalesce(nullif(%s, ''), logo_url),
                careers_url = coalesce(nullif(%s, ''), careers_url),
                employee_count = coalesce(%s, employee_count),
                industries = coalesce(%s, industries),
                tags = coalesce(%s, tags),
                last_funding_round = coalesce(nullif(%s, ''), last_funding_round),
                last_funding_date = coalesce(%s, last_funding_date),
                funding_date_raw = coalesce(nullif(%s, ''), funding_date_raw),
                funding_date_precision = coalesce(nullif(%s, ''), funding_date_precision),
                last_funding_amount = coalesce(%s, last_funding_amount),
                last_funding_currency = coalesce(nullif(%s, ''), last_funding_currency),
                total_funding_usd = coalesce(%s, total_funding_usd),
                ats_type = coalesce(%s, ats_type),
                ats_slug = coalesce(%s, ats_slug),
                ats_board_url = coalesce(%s, ats_board_url),
                ats_detected_via = coalesce(%s, ats_detected_via),
                ats_confidence = coalesce(%s, ats_confidence),
                ats_status = %s,
                ats_sources = %s,
                company_data = %s,
                quality_score = %s,
                is_active = true,
                is_public = %s,
                enrichment_status = %s,
                last_checked_at = now(),
                content_hash = %s,
                last_changed_at = case when %s then now() else last_changed_at end,
                source_synced_at = coalesce(%s, source_synced_at),
                discovery_source = coalesce(discovery_source, %s),
                discovery_url = coalesce(discovery_url, %s)
            where id = %s
            """,
            (
                company.name,
                name_key(company.name),
                slug,
                company.website_url,
                company.domain,
                company.short_description,
                company.founded_year,
                company.headquarters,
                company.country,
                company.city,
                company.linkedin_url,
                company.x_url,
                company.logo_url,
                company.careers_url,
                company.employee_count,
                company.industries or None,
                company.tags or None,
                company.last_funding_round,
                company.last_funding_date,
                company.funding_date_raw,
                company.funding_date_precision,
                company.last_funding_amount,
                company.last_funding_currency,
                company.total_funding_usd,
                board.provider if board else None,
                board.token if board else None,
                board.board_url if board else None,
                board.detected_via if board else None,
                board.confidence if board else None,
                ats_status,
                Jsonb(ats_sources),
                Jsonb(company_data),
                quality_score,
                is_public,
                enrichment_status,
                new_hash,
                changed,
                company.source_synced_at,
                company.discovery_source,
                company.discovery_url,
                company_id,
            ),
        )
        conn.commit()
        return company_id

    slug = _unique_slug(
        conn,
        desired=company.name,
        domain=company.domain,
        name=company.name,
    )
    row = conn.execute(
        """
        insert into public.companies
            (name, normalized_name, slug, website, domain, short_description,
             founded_year, headquarters, country, city, linkedin_url, x_url,
             logo_url, careers_url, employee_count, industries, tags,
             last_funding_round, last_funding_date, funding_date_raw,
             funding_date_precision, last_funding_amount, last_funding_currency,
             total_funding_usd, ats_type, ats_slug, ats_board_url,
             ats_detected_via, ats_confidence, ats_status, ats_sources,
             company_data, quality_score, jobs_count, jobs_status, is_active,
             is_public, enrichment_status, last_checked_at, content_hash,
             last_changed_at, source_synced_at, discovery_source, discovery_url)
        values
            (%s, %s, %s, %s, %s, %s,
             %s, %s, %s, %s, %s, %s,
             %s, %s, %s, %s, %s,
             %s, %s, %s,
             %s, %s, %s,
             %s, %s, %s, %s,
             %s, %s, 0, 'unknown', true,
             %s, %s, now(), %s,
             now(), %s, %s, %s)
        returning id
        """,
        (
            company.name,
            name_key(company.name),
            slug,
            company.website_url,
            company.domain,
            company.short_description,
            company.founded_year,
            company.headquarters,
            company.country,
            company.city,
            company.linkedin_url,
            company.x_url,
            company.logo_url,
            company.careers_url,
            company.employee_count,
            company.industries or None,
            company.tags or None,
            company.last_funding_round,
            company.last_funding_date,
            company.funding_date_raw,
            company.funding_date_precision,
            company.last_funding_amount,
            company.last_funding_currency,
            company.total_funding_usd,
            board.provider if board else None,
            board.token if board else None,
            board.board_url if board else None,
            board.detected_via if board else None,
            board.confidence if board else None,
            ats_status,
            Jsonb(ats_sources),
            Jsonb(company_data),
            quality_score,
            is_public,
            enrichment_status,
            new_hash,
            company.source_synced_at,
            company.discovery_source,
            company.discovery_url,
        ),
    ).fetchone()
    conn.commit()
    return str(row["id"])


def get_company(conn: psycopg.Connection, company_id: str) -> dict[str, Any] | None:
    return conn.execute(
        "select * from public.companies where id = %s", (company_id,)
    ).fetchone()


def upsert_job(
    conn: psycopg.Connection,
    company_id: str,
    board: AtsBoard,
    job: JobRecord,
    *,
    today: date,
) -> tuple[str, bool, bool]:
    source_key = f"{board.provider}|{board.region}|{board.token}|{job.external_id}"
    existing = conn.execute(
        "select * from public.jobs where source_key = %s limit 1", (source_key,)
    ).fetchone()

    published_text = job.published_at.isoformat() if job.published_at else None
    raw_date = job.raw.get("updated_at") or job.raw.get("createdAt") or job.raw.get("publishedAt")
    content = json_hash(
        job.title,
        job.location,
        job.description_text,
        job.apply_url or job.job_url,
        job.employment_type,
        job.workplace_type,
    )
    source_ref = {
        "provider": board.provider,
        "region": board.region,
        "token": board.token,
        "board_url": board.board_url,
        "external_id": job.external_id,
        "url": job.job_url,
    }

    if not existing:
        row = conn.execute(
            """
            insert into public.jobs
                (company_id, source_key, provider, board_url, external_id, title,
                 location, employment_type, workplace_type, job_url, canonical_url,
                 requisition_id, date_posted, published_at_source, status,
                 first_seen_on, last_seen_on, data_hash, raw_data, job_sources,
                 history, description, description_text, department, is_remote,
                 apply_url, published_at, last_seen_at, salary_min, salary_max,
                 salary_currency)
            values
                (%s, %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s,
                 %s, %s, %s, 'available',
                 %s, %s, %s, %s, %s,
                 %s, %s, %s, %s, %s,
                 %s, %s, now(), %s, %s,
                 %s)
            returning id
            """,
            (
                company_id,
                source_key,
                board.provider,
                board.board_url,
                job.external_id,
                job.title,
                job.location,
                job.employment_type,
                job.workplace_type,
                job.job_url,
                job.job_url,
                job.requisition_id,
                published_text,
                str(raw_date or "") or None,
                today,
                today,
                content,
                Jsonb(job.raw or {}),
                Jsonb([source_ref]),
                Jsonb([{"event": "opened", "at": utcnow().isoformat()}]),
                job.description,
                job.description_text,
                job.department,
                job.is_remote,
                job.apply_url or job.job_url,
                job.published_at,
                job.salary_min,
                job.salary_max,
                job.salary_currency,
            ),
        ).fetchone()
        return str(row["id"]), True, False

    changed = (
        existing.get("title") != job.title
        or existing.get("location") != job.location
        or existing.get("description_text") != job.description_text
        or existing.get("status") != "available"
    )
    history = list(existing.get("history") or [])
    if changed:
        history.append(
            {
                "event": "updated" if existing.get("status") == "available" else "reopened",
                "at": utcnow().isoformat(),
                "fields": {
                    "title": [existing.get("title"), job.title],
                    "location": [existing.get("location"), job.location],
                },
            }
        )
        history = history[-30:]
    sources = list(existing.get("job_sources") or [])
    if not any(
        item.get("provider") == board.provider
        and item.get("token") == board.token
        and str(item.get("external_id")) == str(job.external_id)
        for item in sources
        if isinstance(item, dict)
    ):
        sources.append(source_ref)

    conn.execute(
        """
        update public.jobs set
            title = %s,
            location = coalesce(%s, location),
            employment_type = coalesce(%s, employment_type),
            workplace_type = coalesce(%s, workplace_type),
            job_url = %s,
            canonical_url = %s,
            requisition_id = coalesce(%s, requisition_id),
            date_posted = coalesce(%s, date_posted),
            published_at_source = coalesce(%s, published_at_source),
            status = 'available',
            last_seen_on = %s,
            last_seen_at = now(),
            unavailable_on = null,
            closed_at = null,
            data_hash = %s,
            raw_data = %s,
            job_sources = %s,
            history = %s,
            description = coalesce(nullif(%s, ''), description),
            description_text = coalesce(nullif(%s, ''), description_text),
            department = coalesce(%s, department),
            is_remote = coalesce(%s, is_remote),
            apply_url = coalesce(%s, apply_url),
            published_at = coalesce(%s, published_at),
            salary_min = coalesce(%s, salary_min),
            salary_max = coalesce(%s, salary_max),
            salary_currency = coalesce(%s, salary_currency),
            updated_at = now()
        where id = %s
        """,
        (
            job.title,
            job.location,
            job.employment_type,
            job.workplace_type,
            job.job_url,
            job.job_url,
            job.requisition_id,
            published_text,
            str(raw_date or "") or None,
            today,
            content,
            Jsonb(job.raw or {}),
            Jsonb(sources),
            Jsonb(history),
            job.description,
            job.description_text,
            job.department,
            job.is_remote,
            job.apply_url or job.job_url,
            job.published_at,
            job.salary_min,
            job.salary_max,
            job.salary_currency,
            str(existing["id"]),
        ),
    )
    return str(existing["id"]), False, changed


def deactivate_missing(
    conn: psycopg.Connection,
    company_id: str,
    board: AtsBoard,
    seen_source_keys: list[str],
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        update public.jobs set
            status = 'unavailable',
            unavailable_on = current_date,
            closed_at = now(),
            updated_at = now(),
            history = coalesce(history, '[]'::jsonb) || %s::jsonb
        where company_id = %s
          and provider = %s
          and board_url = %s
          and status = 'available'
          and not (source_key = any(%s))
        returning id, title
        """,
        (
            Jsonb(
                [
                    {
                        "event": "closed",
                        "at": utcnow().isoformat(),
                        "board": board.board_url,
                    }
                ]
            ),
            company_id,
            board.provider,
            board.board_url,
            seen_source_keys,
        ),
    ).fetchall()
    return rows


def update_company_job_stats(conn: psycopg.Connection, company_id: str) -> dict[str, int]:
    row = conn.execute(
        """
        select
            count(*) filter (where status = 'available') as active,
            count(*) as total,
            max(last_seen_on) as last_seen
        from public.jobs
        where company_id = %s
        """,
        (company_id,),
    ).fetchone()
    active = int(row["active"] or 0) if row else 0
    total = int(row["total"] or 0) if row else 0
    jobs_status = "hiring" if active > 0 else "not_hiring"
    conn.execute(
        """
        update public.companies set
            jobs_count = %s,
            jobs_status = %s,
            last_jobs_synced_at = now(),
            last_checked_at = now()
        where id = %s
        """,
        (active, jobs_status, company_id),
    )
    return {"active": active, "total": total}


def add_review(
    conn: psycopg.Connection,
    *,
    run_id: str,
    run_day: date,
    company_id: str | None,
    name: str,
    domain: str | None,
    website: str | None,
    reason_code: str,
    details: str,
    raw: dict[str, Any] | None = None,
) -> str:
    dedupe_key = f"{domain or name_key(name) or 'unknown'}:{reason_code}"
    raw_json: dict[str, Any]
    try:
        encoded = json.dumps(raw or {}, ensure_ascii=False, default=str)
        if len(encoded) > 20_000:
            raw_json = {"truncated": True, "preview": encoded[:20_000]}
        else:
            raw_json = raw or {}
    except (TypeError, ValueError):
        raw_json = {"error": "unserializable raw payload"}

    row = conn.execute(
        """
        insert into public.reviews
            (run_id, run_day, company_id, name, domain, slug, website,
             reason_code, details, raw_json, attempts, review_status, dedupe_key)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, 'pending', %s)
        on conflict (dedupe_key) do update set
            run_id = coalesce(excluded.run_id, reviews.run_id),
            run_day = coalesce(excluded.run_day, reviews.run_day),
            company_id = coalesce(reviews.company_id, excluded.company_id),
            name = excluded.name,
            domain = coalesce(excluded.domain, reviews.domain),
            website = coalesce(excluded.website, reviews.website),
            details = excluded.details,
            raw_json = excluded.raw_json,
            attempts = reviews.attempts + 1,
            review_status = 'pending',
            updated_at = now()
        returning id
        """,
        (
            run_id,
            run_day,
            company_id,
            name,
            domain,
            slugify(name),
            website,
            reason_code,
            details,
            Jsonb(raw_json),
            dedupe_key,
        ),
    ).fetchone()
    conn.commit()
    return str(row["id"])


def get_refresh_companies(conn: psycopg.Connection, limit: int) -> list[dict[str, Any]]:
    return conn.execute(
        """
        select *
        from public.companies
        where is_active
          and ats_type is not null
          and ats_slug is not null
          and (
                (jobs_count > 0 and (last_jobs_synced_at is null
                    or last_jobs_synced_at < now() - interval '3 days'))
             or (jobs_count = 0 and (last_jobs_synced_at is null
                    or last_jobs_synced_at < now() - interval '14 days'))
          )
        order by
            case when jobs_count > 0 then 0 else 1 end,
            last_jobs_synced_at nulls first,
            created_at
        limit %s
        """,
        (limit,),
    ).fetchall()


def report_totals(conn: psycopg.Connection) -> dict[str, int]:
    row = conn.execute(
        """
        select
            (select count(*) from public.companies where is_active) as companies,
            (select count(*) from public.jobs where status = 'available') as jobs_active,
            (select count(*) from public.jobs where status = 'unavailable') as jobs_closed,
            (select count(*) from public.reviews where review_status = 'pending') as reviews_pending
        """
    ).fetchone()
    return {key: int(value or 0) for key, value in (row or {}).items()}