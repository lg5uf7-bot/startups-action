from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from rapidfuzz import fuzz

from ..http import HttpClient, HttpError
from ..models import Candidate
from ..normalize import (
    date_precision,
    name_key,
    parse_date,
    parse_datetime,
    parse_money,
    safe_domain,
)

log = logging.getLogger(__name__)

BASE = "https://startupdb.com/api/v1"
ATTRIBUTION = "StartupDB (https://startupdb.com), CC BY 4.0"


class StartupDB:
    """Enrichment client.

    StartupDB search is a name/domain lookup, not an industry search. It is used
    only after a company has been discovered somewhere else.
    """

    def __init__(self, client: HttpClient, delay: float = 0.55) -> None:
        self.client = client
        self.delay = delay
        self._search_cache: dict[str, list[dict[str, Any]]] = {}
        self._detail_cache: dict[str, dict[str, Any]] = {}

    def search(self, query: str) -> list[dict[str, Any]]:
        query = str(query or "").strip()
        if not query:
            return []
        cache_key = query.casefold()
        if cache_key in self._search_cache:
            return self._search_cache[cache_key]
        url = f"{BASE}/startups?q={quote(query)}&limit=5"
        try:
            data = self.client.get_json(url, delay=self.delay)
        except HttpError as exc:
            log.debug("StartupDB search failed for %s: %s", query, exc)
            self._search_cache[cache_key] = []
            return []
        rows = data.get("data", []) if isinstance(data, dict) else []
        rows = [row for row in rows if isinstance(row, dict)]
        self._search_cache[cache_key] = rows
        return rows

    def detail(self, slug: str) -> dict[str, Any] | None:
        slug = str(slug or "").strip()
        if not slug:
            return None
        if slug in self._detail_cache:
            return self._detail_cache[slug]
        try:
            data = self.client.get_json(f"{BASE}/startups/{quote(slug)}", delay=self.delay)
        except HttpError as exc:
            log.debug("StartupDB detail failed for %s: %s", slug, exc)
            return None
        record = data.get("data") if isinstance(data, dict) else None
        if isinstance(record, dict):
            self._detail_cache[slug] = record
            return record
        return None

    def find(self, candidate: Candidate) -> dict[str, Any] | None:
        queries: list[str] = []
        if candidate.domain:
            queries.append(candidate.domain)
        if candidate.name:
            queries.append(candidate.name)
        if candidate.ats_slug and candidate.ats_slug not in queries:
            queries.append(candidate.ats_slug)

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for query in queries:
            for row in self.search(query):
                slug = str(row.get("slug") or "")
                if slug and slug not in seen:
                    seen.add(slug)
                    results.append(row)

        best: tuple[float, dict[str, Any]] | None = None
        scored: list[tuple[float, dict[str, Any]]] = []
        for row in results:
            score = 0.0
            row_domain = row.get("domain") or safe_domain(row.get("websiteUrl"))
            if candidate.domain and row_domain and candidate.domain.casefold() == str(row_domain).casefold():
                score = 1.0
            else:
                ratio = fuzz.ratio(name_key(candidate.name), name_key(row.get("name"))) / 100.0
                score = ratio * 0.95
                if candidate.ats_slug and name_key(candidate.ats_slug) == name_key(row.get("name")):
                    score = max(score, 0.9)
            scored.append((score, row))
            if score >= 0.86 and (best is None or score > best[0]):
                best = (score, row)

        if not best:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.03:
            if str(scored[0][1].get("slug")) != str(scored[1][1].get("slug")):
                log.info("StartupDB ambiguous match for %s", candidate.name)
                return None
        return best[1]

    def enrich(self, candidate: Candidate) -> dict[str, Any]:
        match = self.find(candidate)
        if not match:
            return {}
        detail = self.detail(str(match.get("slug") or ""))
        if not detail:
            return {}
        return map_detail(detail)


def _best_description(profile: dict[str, Any]) -> str | None:
    if profile.get("shortDescription"):
        return str(profile["shortDescription"]).strip()
    descriptions = [
        item
        for item in (profile.get("sourceDescriptions") or [])
        if isinstance(item, dict) and str(item.get("value") or "").strip()
    ]
    descriptions.sort(key=lambda item: 0 if item.get("attribution") == "human_reviewed" else 1)
    if descriptions:
        return str(descriptions[0]["value"]).strip()
    return None


def _industry_values(profile: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("sourceIndustryLabels", "sectorTags", "productTechnologyTags"):
        for item in profile.get(key) or []:
            if isinstance(item, dict):
                value = str(item.get("value") or "").strip()
            else:
                value = str(item or "").strip()
            if value and value.casefold() not in {v.casefold() for v in values}:
                values.append(value)
    return values


def map_detail(detail: dict[str, Any]) -> dict[str, Any]:
    profile = detail.get("profile") or {}
    website = str(detail.get("websiteUrl") or "").strip() or None
    domain = str(detail.get("domain") or "").strip() or safe_domain(website)
    latest = detail.get("latestFunding") or {}
    amount = latest.get("amount") or {}
    funding_date_raw = detail.get("latestFundingDate") or latest.get("eventDate")
    funding_amount, funding_currency = parse_money(amount.get("original"))
    total_funding, _ = parse_money(detail.get("fundingTotalRaised"))

    return {
        "name": str(detail.get("name") or "").strip() or None,
        "domain": domain,
        "website_url": website,
        "short_description": _best_description(profile),
        "founded_year": profile.get("foundedYear"),
        "headquarters": profile.get("headquartersLocation"),
        "logo_url": profile.get("brandAssetUrl"),
        "linkedin_url": detail.get("linkedinUrl"),
        "x_url": detail.get("xUrl"),
        "industries": _industry_values(profile),
        "last_funding_round": latest.get("roundLabel"),
        "last_funding_date": parse_date(funding_date_raw),
        "funding_date_raw": str(funding_date_raw) if funding_date_raw else None,
        "funding_date_precision": date_precision(funding_date_raw),
        "last_funding_amount": funding_amount,
        "last_funding_currency": funding_currency or amount.get("currency"),
        "total_funding_usd": total_funding,
        "source_synced_at": parse_datetime(detail.get("lastSyncedAt")),
        "startupdb_slug": detail.get("slug"),
        "raw": detail,
        "attribution": ATTRIBUTION,
    }