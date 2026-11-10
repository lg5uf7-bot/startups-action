from __future__ import annotations

import html as html_lib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

from ..http import HttpClient, HttpError
from ..models import AtsBoard, FetchResult, JobRecord
from ..normalize import (
    clean_text,
    content_hash,
    infer_remote,
    normalize_employment_type,
    parse_datetime,
    truncate,
)
from .patterns import board_url

log = logging.getLogger(__name__)

MAX_JOBS_PER_BOARD = 5000
MAX_DESCRIPTION_CHARS = 20_000


@dataclass
class ProbeResult:
    ok: bool
    status: int | None = None
    name: str | None = None
    error_code: str | None = None


def _location_text(value: Any) -> str | None:
    if isinstance(value, dict):
        parts = [
            str(value.get(key) or "").strip()
            for key in ("name", "city", "region", "country", "state")
        ]
        text = ", ".join(part for part in parts if part)
        return text or None
    text = str(value or "").strip()
    return text or None


def _first_department(row: dict[str, Any]) -> str | None:
    departments = row.get("departments") or []
    if isinstance(departments, list):
        for department in departments:
            if isinstance(department, dict) and department.get("name"):
                return str(department["name"]).strip()
            if isinstance(department, str) and department.strip():
                return department.strip()
    department = row.get("department")
    if isinstance(department, dict):
        return str(department.get("name") or "").strip() or None
    return str(department or "").strip() or None


def _metadata_value(row: dict[str, Any], *names: str) -> str | None:
    wanted = {name.casefold() for name in names}
    for item in row.get("metadata") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "").casefold() in wanted:
            value = str(item.get("value") or "").strip()
            if value:
                return value
    return None


def _salary_from_range(value: Any) -> tuple[float | None, float | None, str | None]:
    if not isinstance(value, dict):
        return None, None, None
    minimum = value.get("min") or value.get("minimum")
    maximum = value.get("max") or value.get("maximum")
    currency = value.get("currency")
    try:
        minimum = float(minimum) if minimum is not None else None
    except (TypeError, ValueError):
        minimum = None
    try:
        maximum = float(maximum) if maximum is not None else None
    except (TypeError, ValueError):
        maximum = None
    return minimum, maximum, str(currency).upper() if currency else None


def _finish(
    *,
    external_id: Any,
    title: Any,
    job_url: Any,
    raw: dict[str, Any],
    apply_url: Any = None,
    location: Any = None,
    department: Any = None,
    employment_type: Any = None,
    workplace_type: Any = None,
    is_remote: Any = None,
    description: Any = None,
    published_at: Any = None,
    requisition_id: Any = None,
    salary_min: Any = None,
    salary_max: Any = None,
    salary_currency: Any = None,
) -> JobRecord | None:
    external = str(external_id or "").strip()
    title_text = str(title or "").strip()
    url = str(job_url or apply_url or "").strip()
    if not external or not title_text or not url:
        return None
    description_html = truncate(description, MAX_DESCRIPTION_CHARS)
    description_text = clean_text(description_html, max_chars=MAX_DESCRIPTION_CHARS)
    return JobRecord(
        external_id=external,
        title=title_text[:500],
        job_url=url,
        apply_url=str(apply_url or "").strip() or None,
        location=_location_text(location),
        department=str(department or "").strip()[:300] or None,
        employment_type=normalize_employment_type(employment_type),
        workplace_type=str(workplace_type or "").strip()[:60] or None,
        is_remote=infer_remote(location, workplace_type, is_remote),
        description=description_html,
        description_text=description_text,
        published_at=parse_datetime(published_at),
        salary_min=float(salary_min) if salary_min is not None else None,
        salary_max=float(salary_max) if salary_max is not None else None,
        salary_currency=str(salary_currency or "").upper() or None,
        requisition_id=str(requisition_id or "").strip() or None,
        raw=raw,
    )


def normalize_greenhouse_row(row: dict[str, Any]) -> JobRecord | None:
    if not isinstance(row, dict):
        return None
    return _finish(
        external_id=row.get("id"),
        title=row.get("title"),
        job_url=row.get("absolute_url"),
        apply_url=row.get("absolute_url"),
        location=row.get("location"),
        department=_first_department(row),
        employment_type=_metadata_value(row, "Employment Type", "Employment type"),
        workplace_type=_metadata_value(row, "Workplace Type", "Workplace type", "Location Type"),
        is_remote=row.get("is_remote"),
        description=html_lib.unescape(str(row.get("content") or "")),
        published_at=row.get("first_published") or row.get("updated_at"),
        requisition_id=row.get("requisition_id"),
        raw=row,
    )


def normalize_lever_row(row: dict[str, Any]) -> JobRecord | None:
    if not isinstance(row, dict):
        return None
    categories = row.get("categories") or {}
    salary_min, salary_max, salary_currency = _salary_from_range(row.get("salaryRange"))
    return _finish(
        external_id=row.get("id"),
        title=row.get("text"),
        job_url=row.get("hostedUrl"),
        apply_url=row.get("applyUrl"),
        location=categories.get("location") if isinstance(categories, dict) else None,
        department=categories.get("team") if isinstance(categories, dict) else None,
        employment_type=categories.get("commitment") if isinstance(categories, dict) else None,
        workplace_type=row.get("workplaceType"),
        is_remote=row.get("isRemote"),
        description=row.get("descriptionPlain") or row.get("description"),
        published_at=row.get("createdAt"),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        raw=row,
    )


def normalize_ashby_row(row: dict[str, Any]) -> JobRecord | None:
    if not isinstance(row, dict):
        return None
    compensation = row.get("compensation") or {}
    summary = compensation.get("compensationTierSummary") if isinstance(compensation, dict) else None
    salary_min = salary_max = None
    salary_currency = None
    if isinstance(compensation, dict):
        salary_min, salary_max, salary_currency = _salary_from_range(compensation)
    job_url = row.get("jobUrl") or row.get("applyUrl")
    external = row.get("id")
    if not external and job_url:
        external = str(job_url).rstrip("/").split("/")[-1]
    return _finish(
        external_id=external,
        title=row.get("title"),
        job_url=job_url,
        apply_url=row.get("applyUrl"),
        location=row.get("location"),
        department=row.get("department") or row.get("team"),
        employment_type=row.get("employmentType"),
        workplace_type=row.get("workplaceType"),
        is_remote=row.get("isRemote"),
        description=row.get("descriptionPlain") or row.get("descriptionHtml"),
        published_at=row.get("publishedAt"),
        salary_min=salary_min,
        salary_max=salary_max,
        salary_currency=salary_currency,
        raw={**row, "_compensationSummary": summary} if summary else row,
    )


def normalize_workable_row(row: dict[str, Any]) -> JobRecord | None:
    if not isinstance(row, dict):
        return None
    location = row.get("location") or {}
    return _finish(
        external_id=row.get("id") or row.get("shortcode"),
        title=row.get("title"),
        job_url=row.get("url") or row.get("application_url"),
        apply_url=row.get("application_url"),
        location=location,
        department=row.get("department"),
        employment_type=row.get("employment_type"),
        workplace_type=row.get("workplace"),
        is_remote=row.get("telecommuting"),
        description=row.get("description"),
        published_at=row.get("published_on"),
        raw=row,
    )


def normalize_smartrecruiters_row(row: dict[str, Any], token: str) -> JobRecord | None:
    if not isinstance(row, dict):
        return None
    job_ad = row.get("jobAd") or {}
    sections = job_ad.get("sections") if isinstance(job_ad, dict) else {}
    description = None
    if isinstance(sections, dict):
        job_description = sections.get("jobDescription") or {}
        if isinstance(job_description, dict):
            description = job_description.get("text")
    job_id = row.get("id") or row.get("ref")
    job_url = f"https://jobs.smartrecruiters.com/{quote(str(token))}/{quote(str(job_id))}"
    return _finish(
        external_id=job_id,
        title=row.get("name"),
        job_url=job_url,
        apply_url=job_url,
        location=row.get("location"),
        department=row.get("department"),
        employment_type=(row.get("typeOfEmployment") or {}).get("label")
        if isinstance(row.get("typeOfEmployment"), dict)
        else row.get("typeOfEmployment"),
        description=description,
        published_at=row.get("releasedDate"),
        raw=row,
    )


def fetch_jobs(client: HttpClient, board: AtsBoard) -> FetchResult:
    provider = board.provider
    token = board.token
    try:
        if provider == "greenhouse":
            api = f"https://boards-api.greenhouse.io/v1/boards/{quote(token)}/jobs?content=true"
            data = client.get_json(api)
            rows = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                return FetchResult(ok=False, error_code="schema_changed", error="missing jobs[]")
            jobs = [job for row in rows[:MAX_JOBS_PER_BOARD] if (job := normalize_greenhouse_row(row))]
            return FetchResult(ok=True, jobs=jobs, http_status=200)

        if provider == "lever":
            api_host = "api.eu.lever.co" if board.region == "eu" else "api.lever.co"
            jobs: list[JobRecord] = []
            for page in range(10):
                api = (
                    f"https://{api_host}/v0/postings/{quote(token)}"
                    f"?mode=json&limit=100&skip={page * 100}"
                )
                data = client.get_json(api)
                if not isinstance(data, list):
                    return FetchResult(ok=False, error_code="schema_changed", error="expected list")
                for row in data:
                    job = normalize_lever_row(row)
                    if job:
                        jobs.append(job)
                if len(data) < 100 or len(jobs) >= MAX_JOBS_PER_BOARD:
                    break
            return FetchResult(ok=True, jobs=jobs[:MAX_JOBS_PER_BOARD], http_status=200)

        if provider == "ashby":
            api = (
                "https://api.ashbyhq.com/posting-api/job-board/"
                f"{quote(token)}?includeCompensation=true"
            )
            data = client.get_json(api)
            rows = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                return FetchResult(ok=False, error_code="schema_changed", error="missing jobs[]")
            jobs = [job for row in rows[:MAX_JOBS_PER_BOARD] if (job := normalize_ashby_row(row))]
            return FetchResult(ok=True, jobs=jobs, http_status=200)

        if provider == "workable":
            api = f"https://apply.workable.com/api/v1/widget/accounts/{quote(token)}?details=true"
            data = client.get_json(api)
            rows = data.get("jobs") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                return FetchResult(ok=False, error_code="schema_changed", error="missing jobs[]")
            jobs = [job for row in rows[:MAX_JOBS_PER_BOARD] if (job := normalize_workable_row(row))]
            return FetchResult(ok=True, jobs=jobs, http_status=200)

        if provider == "smartrecruiters":
            jobs: list[JobRecord] = []
            offset = 0
            while offset < MAX_JOBS_PER_BOARD:
                api = (
                    f"https://api.smartrecruiters.com/v1/companies/{quote(token)}"
                    f"/postings?limit=100&offset={offset}"
                )
                data = client.get_json(api)
                rows = data.get("content") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    return FetchResult(ok=False, error_code="schema_changed", error="missing content[]")
                for row in rows:
                    job = normalize_smartrecruiters_row(row, token)
                    if job:
                        jobs.append(job)
                total = int(data.get("totalFound") or 0)
                offset += len(rows)
                if not rows or offset >= total:
                    break
            return FetchResult(ok=True, jobs=jobs, http_status=200)

        return FetchResult(ok=False, error_code="unsupported_provider", error=provider)
    except HttpError as exc:
        return FetchResult(
            ok=False,
            error_code=exc.code,
            http_status=exc.status,
            error=str(exc)[:300],
        )
    except Exception as exc:  # defensive: a bad payload must not kill the run
        log.exception("Unexpected %s error for %s/%s", provider, token, exc)
        return FetchResult(ok=False, error_code="parse_error", error=str(exc)[:300])


def probe(client: HttpClient, provider: str, token: str, region: str = "global") -> ProbeResult:
    try:
        if provider == "greenhouse":
            data = client.get_json(
                f"https://boards-api.greenhouse.io/v1/boards/{quote(token)}",
                allow_404=True,
            )
            if data is None:
                return ProbeResult(ok=False, status=404, error_code="not_found")
            return ProbeResult(ok=True, status=200, name=str((data or {}).get("name") or "") or None)
        if provider == "lever":
            api_host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
            data = client.get_json(
                f"https://{api_host}/v0/postings/{quote(token)}?mode=json&limit=1",
                allow_404=True,
            )
            if data is None:
                return ProbeResult(ok=False, status=404, error_code="not_found")
            if not isinstance(data, list):
                return ProbeResult(ok=False, status=200, error_code="schema_changed")
            return ProbeResult(ok=True, status=200)
        if provider == "ashby":
            data = client.get_json(
                "https://api.ashbyhq.com/posting-api/job-board/"
                f"{quote(token)}?includeCompensation=false",
                allow_404=True,
            )
            if data is None:
                return ProbeResult(ok=False, status=404, error_code="not_found")
            if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
                return ProbeResult(ok=False, status=200, error_code="schema_changed")
            return ProbeResult(ok=True, status=200)
        if provider == "workable":
            data = client.get_json(
                f"https://apply.workable.com/api/v1/widget/accounts/{quote(token)}",
                allow_404=True,
            )
            if data is None:
                return ProbeResult(ok=False, status=404, error_code="not_found")
            if not isinstance(data, dict):
                return ProbeResult(ok=False, status=200, error_code="schema_changed")
            return ProbeResult(ok=True, status=200, name=str(data.get("name") or "") or None)
        if provider == "smartrecruiters":
            data = client.get_json(
                f"https://api.smartrecruiters.com/v1/companies/{quote(token)}/postings?limit=1",
                allow_404=True,
            )
            if data is None:
                return ProbeResult(ok=False, status=404, error_code="not_found")
            if not isinstance(data, dict):
                return ProbeResult(ok=False, status=200, error_code="schema_changed")
            return ProbeResult(ok=True, status=200)
        return ProbeResult(ok=False, error_code="unsupported_provider")
    except HttpError as exc:
        return ProbeResult(ok=False, status=exc.status, error_code=exc.code)
    except Exception as exc:  # pragma: no cover - defensive
        return ProbeResult(ok=False, error_code="probe_error")