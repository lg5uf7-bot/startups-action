from __future__ import annotations

import logging
from typing import Any

from bs4 import BeautifulSoup

from ..ats.patterns import extract_ats
from ..http import HttpClient, HttpError
from ..models import Candidate
from ..normalize import name_key, safe_domain, valid_http_url

log = logging.getLogger(__name__)

API_URL = "https://remotive.com/api/remote-jobs?limit=100"


def _links_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    return [
        str(anchor.get("href")).strip()
        for anchor in soup.find_all("a")
        if anchor.get("href") and valid_http_url(anchor.get("href"))
    ]


def fetch(client: HttpClient, limit: int = 80) -> list[Candidate]:
    try:
        data = client.get_json(API_URL)
    except HttpError as exc:
        log.warning("Remotive failed: %s", exc)
        return []
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    grouped: dict[str, dict[str, Any]] = {}
    for job in jobs:
        if not isinstance(job, dict):
            continue
        company = str(job.get("company_name") or "").strip()
        if not company:
            continue
        key = name_key(company)
        if key not in grouped:
            grouped[key] = {"company": company, "jobs": []}
        grouped[key]["jobs"].append(job)

    out: list[Candidate] = []
    for entry in grouped.values():
        company = entry["company"]
        jobs_for_company = entry["jobs"]
        first = jobs_for_company[0]
        links: list[str] = []
        for job in jobs_for_company:
            links.extend(_links_from_html(str(job.get("description") or "")))
        ats = extract_ats(" ".join(links))
        website = next((link for link in links if safe_domain(link)), None)
        domain = safe_domain(website)
        key = f"domain:{domain}" if domain else f"remotive:{name_key(company)}"
        out.append(
            Candidate(
                company_key=key,
                name=company,
                domain=domain,
                website_url=website,
                ats_type=ats[0] if ats else None,
                ats_slug=ats[1] if ats else None,
                discovery_source="remotive",
                discovery_url=str(first.get("url") or API_URL),
                raw={
                    "source": "remotive",
                    "company": company,
                    "job_count": len(jobs_for_company),
                    "sample": first,
                },
            )
        )
        if len(out) >= limit:
            break
    return out