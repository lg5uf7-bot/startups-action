from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any

from ..http import HttpClient, HttpError
from ..models import Candidate
from ..normalize import is_probably_company_name, name_key, safe_domain

log = logging.getLogger(__name__)

OPEN_JOBS_URL = "https://raw.githubusercontent.com/ConorsCode/open-jobs-data/main/companies.json"
OPEN_ATS_URL = "https://raw.githubusercontent.com/veresk06/open-ats-feed/main/actor/data/companies.json"
TECHSTARS_URL = "https://raw.githubusercontent.com/yigitmeteozcan/startups/refs/heads/main/data/all.json"

ATS_PROVIDERS = ("greenhouse", "ashby", "lever")


def humanize_token(token: str) -> str:
    text = re.sub(r"[-_]+", " ", token or "").strip()
    text = re.sub(r"\s+", " ", text)
    if not text:
        return token or "Unknown"
    return " ".join(word.capitalize() if word.islower() else word for word in text.split())


def _rotate(rows: list[Any], day: date, limit: int, multiplier: int) -> list[Any]:
    if not rows or limit <= 0:
        return []
    start = (day.timetuple().tm_yday * multiplier) % len(rows)
    if start + limit <= len(rows):
        return rows[start : start + limit]
    return rows[start:] + rows[: limit - (len(rows) - start)]


def fetch_open_jobs_catalog(client: HttpClient) -> list[Candidate]:
    """378 community-maintained name -> ATS token mappings."""
    try:
        rows = client.get_json(OPEN_JOBS_URL)
    except HttpError as exc:
        log.warning("open-jobs catalog failed: %s", exc)
        return []
    if not isinstance(rows, list):
        return []
    out: list[Candidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        provider = str(row.get("platform") or "").strip().lower()
        token = str(row.get("slug") or "").strip()
        if not name or provider not in ATS_PROVIDERS or not token:
            continue
        out.append(
            Candidate(
                company_key=f"ats:{provider}:{token}",
                name=name,
                ats_type=provider,
                ats_slug=token,
                discovery_source="open_jobs_catalog",
                discovery_url=OPEN_JOBS_URL,
                raw={"catalog": "open_jobs", "row": row},
            )
        )
    return out


def fetch_ats_feed(client: HttpClient, day: date, limit: int = 200) -> list[Candidate]:
    """Live ATS board tokens harvested from public board APIs."""
    try:
        data = client.get_json(OPEN_ATS_URL)
    except HttpError as exc:
        log.warning("open ATS feed failed: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    providers = data.get("providers") or {}
    rows: list[tuple[str, str, int]] = []
    for provider in ATS_PROVIDERS:
        group = providers.get(provider) or {}
        for item in group.get("live") or []:
            if isinstance(item, list) and item:
                token = str(item[0] or "").strip()
                count = int(item[1] or 0) if len(item) > 1 else 0
            elif isinstance(item, str):
                token = item.strip()
                count = 0
            else:
                continue
            if token:
                rows.append((provider, token, count))
    out: list[Candidate] = []
    for provider, token, count in _rotate(rows, day, limit, 211):
        out.append(
            Candidate(
                company_key=f"ats:{provider}:{token}",
                name=humanize_token(token),
                ats_type=provider,
                ats_slug=token,
                discovery_source="open_ats_feed",
                discovery_url=OPEN_ATS_URL,
                raw={"provider": provider, "token": token, "postings": count},
            )
        )
    return out


def fetch_techstars(client: HttpClient, day: date, limit: int = 120) -> list[Candidate]:
    """Broad company source: 15k+ accelerator companies with descriptions and logos."""
    try:
        rows = client.get_json(TECHSTARS_URL)
    except HttpError as exc:
        log.warning("techstars feed failed: %s", exc)
        return []
    if not isinstance(rows, list):
        return []
    valid = [
        row
        for row in rows
        if isinstance(row, dict)
        and is_probably_company_name(row.get("name"))
        and (safe_domain(row.get("website")) or row.get("website"))
    ]
    out: list[Candidate] = []
    for row in _rotate(valid, day, limit, 97):
        name = str(row.get("name") or "").strip()
        website = str(row.get("website") or "").strip() or None
        domain = safe_domain(website)
        key = f"domain:{domain}" if domain else f"feed:{name_key(name)}"
        out.append(
            Candidate(
                company_key=key,
                name=name,
                domain=domain,
                website_url=website,
                discovery_source="techstars_feed",
                discovery_url="https://github.com/yigitmeteozcan/startups",
                raw={"feed": "techstars", "record": row},
            )
        )
    return out