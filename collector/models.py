from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass
class Candidate:
    company_key: str
    name: str
    domain: str | None = None
    website_url: str | None = None
    ats_type: str | None = None
    ats_slug: str | None = None
    discovery_source: str = ""
    discovery_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class AtsBoard:
    provider: str
    token: str
    board_url: str
    api_url: str
    confidence: float = 0.0
    detected_via: str = ""
    region: str = "global"
    name: str | None = None


@dataclass
class JobRecord:
    external_id: str
    title: str
    job_url: str
    apply_url: str | None = None
    location: str | None = None
    department: str | None = None
    employment_type: str | None = None
    workplace_type: str | None = None
    is_remote: bool | None = None
    description: str | None = None
    description_text: str | None = None
    published_at: datetime | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    requisition_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchResult:
    ok: bool
    jobs: list[JobRecord] = field(default_factory=list)
    error_code: str | None = None
    http_status: int | None = None
    error: str | None = None


@dataclass
class CompanyRecord:
    name: str
    domain: str | None = None
    website_url: str | None = None
    slug: str | None = None
    short_description: str | None = None
    founded_year: int | None = None
    headquarters: str | None = None
    country: str | None = None
    city: str | None = None
    linkedin_url: str | None = None
    x_url: str | None = None
    logo_url: str | None = None
    careers_url: str | None = None
    employee_count: int | None = None
    industries: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    last_funding_round: str | None = None
    last_funding_date: date | None = None
    funding_date_raw: str | None = None
    funding_date_precision: str | None = None
    last_funding_amount: float | None = None
    last_funding_currency: str | None = None
    total_funding_usd: float | None = None
    discovery_source: str | None = None
    discovery_url: str | None = None
    source_synced_at: datetime | None = None
    raw_sources: list[dict[str, Any]] = field(default_factory=list)