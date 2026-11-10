from __future__ import annotations

import logging
from datetime import date

from ..http import HttpClient, HttpError
from ..models import Candidate
from ..normalize import name_key, safe_domain

log = logging.getLogger(__name__)

API_URL = "https://api.ycombinator.com/v0.1/companies?page={page}"


def fetch(client: HttpClient, day: date, limit: int = 100) -> list[Candidate]:
    try:
        first = client.get_json(API_URL.format(page=1))
    except HttpError as exc:
        log.warning("YC API failed: %s", exc)
        return []
    total_pages = int(first.get("totalPages") or 1) if isinstance(first, dict) else 1
    page = (day.timetuple().tm_yday % max(total_pages, 1)) + 1
    try:
        data = first if page == 1 else client.get_json(API_URL.format(page=page))
    except HttpError as exc:
        log.warning("YC page failed: %s", exc)
        return []
    rows = data.get("companies", []) if isinstance(data, dict) else []
    out: list[Candidate] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "").lower() not in {"active", ""}:
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        website = str(row.get("website") or "").strip() or None
        domain = safe_domain(website)
        slug = str(row.get("slug") or name_key(name))
        key = f"domain:{domain}" if domain else f"yc:{slug}"
        out.append(
            Candidate(
                company_key=key,
                name=name,
                domain=domain,
                website_url=website,
                discovery_source="yc",
                discovery_url=str(row.get("url") or "https://www.ycombinator.com/companies"),
                raw={"source": "yc", "record": row},
            )
        )
        if len(out) >= limit:
            break
    return out