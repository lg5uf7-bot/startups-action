from __future__ import annotations

import logging

from ..ats.patterns import extract_ats
from ..http import HttpClient, HttpError
from ..models import Candidate
from ..normalize import name_key, safe_domain

log = logging.getLogger(__name__)

API_URL = "https://remoteok.com/api"


def fetch(client: HttpClient, limit: int = 100) -> list[Candidate]:
    try:
        rows = client.get_json(API_URL)
    except HttpError as exc:
        log.warning("RemoteOK failed: %s", exc)
        return []
    if not isinstance(rows, list):
        return []
    out: list[Candidate] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or "legal" in row:
            continue
        company = str(row.get("company") or "").strip()
        if not company:
            continue
        apply_url = str(row.get("apply_url") or "").strip()
        job_url = str(row.get("url") or "").strip()
        ats = extract_ats(apply_url) or extract_ats(job_url) or extract_ats(row.get("description") or "")
        website = None
        if safe_domain(apply_url):
            website = apply_url
        elif safe_domain(job_url):
            website = job_url
        domain = safe_domain(website)
        key = f"domain:{domain}" if domain else f"remoteok:{name_key(company)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(
            Candidate(
                company_key=key,
                name=company,
                domain=domain,
                website_url=website,
                ats_type=ats[0] if ats else None,
                ats_slug=ats[1] if ats else None,
                discovery_source="remoteok",
                discovery_url=job_url or apply_url or API_URL,
                raw={"source": "remoteok", "job": row},
            )
        )
        if len(out) >= limit:
            break
    return out