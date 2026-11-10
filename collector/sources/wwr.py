from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from ..ats.patterns import extract_ats
from ..http import HttpClient, HttpError
from ..models import Candidate
from ..normalize import name_key, safe_domain, valid_http_url

log = logging.getLogger(__name__)

FEEDS = [
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
    "https://weworkremotely.com/categories/remote-design-jobs.rss",
    "https://weworkremotely.com/categories/remote-marketing-jobs.rss",
]


def _company_from_title(title: str) -> str:
    text = re.sub(r"\s+", " ", title or "").strip()
    if ":" in text:
        return text.split(":", 1)[0].strip()[:120]
    match = re.match(r"(.+?)\s+is\s+hiring", text, flags=re.IGNORECASE)
    if match:
        return match.group(1).strip()[:120]
    return text[:120]


def _links_from_html(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    return [
        str(anchor.get("href")).strip()
        for anchor in soup.find_all("a")
        if anchor.get("href") and valid_http_url(anchor.get("href"))
    ]


def fetch(client: HttpClient, limit: int = 80) -> list[Candidate]:
    out: list[Candidate] = []
    seen: set[str] = set()
    for feed in FEEDS:
        try:
            response = client.get_text(feed)
        except HttpError as exc:
            log.warning("WWR feed failed %s: %s", feed, exc)
            continue
        try:
            root = ET.fromstring(response.text)
        except ET.ParseError:
            continue
        for item in root.findall(".//item"):
            title = item.findtext("title") or ""
            link = item.findtext("link") or ""
            description = item.findtext("description") or ""
            company = _company_from_title(title)
            if not company:
                continue
            links = _links_from_html(description)
            ats = extract_ats(" ".join(links)) or extract_ats(description)
            website = next((value for value in links if safe_domain(value)), None)
            domain = safe_domain(website)
            key = f"domain:{domain}" if domain else f"wwr:{name_key(company)}"
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
                    discovery_source="weworkremotely",
                    discovery_url=link,
                    raw={
                        "source": "weworkremotely",
                        "title": title,
                        "link": link,
                        "description": description[:5000],
                    },
                )
            )
            if len(out) >= limit:
                return out
    return out