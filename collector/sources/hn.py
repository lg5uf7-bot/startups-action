from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from ..ats.patterns import extract_ats
from ..http import HttpClient, HttpError
from ..models import Candidate
from ..normalize import name_key, safe_domain, valid_http_url

log = logging.getLogger(__name__)

SEARCH_URL = (
    "https://hn.algolia.com/api/v1/search_by_date"
    "?tags=story,author_whoishiring&hitsPerPage=10"
)
ITEM_URL = "https://hn.algolia.com/api/v1/items/{id}"


def latest_hiring_thread(client: HttpClient) -> str | None:
    try:
        data = client.get_json(SEARCH_URL)
    except HttpError as exc:
        log.warning("HN search failed: %s", exc)
        return None
    for hit in data.get("hits", []) if isinstance(data, dict) else []:
        title = str(hit.get("title") or "")
        if title.lower().startswith("ask hn: who is hiring?"):
            return str(hit.get("objectID") or "") or None
    return None


def _clean_company_name(value: str) -> str:
    text = re.sub(r"\s+", " ", value or "").strip(" |-")
    text = re.sub(r"\s+(is\s+)?hiring.*$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*\(.*?\)\s*$", "", text)
    return text.strip()[:120]


def parse_comment(comment: dict[str, Any], thread_id: str) -> Candidate | None:
    html = comment.get("text") or ""
    if not html or comment.get("deleted"):
        return None
    soup = BeautifulSoup(html, "html.parser")
    links = [
        str(anchor.get("href")).strip()
        for anchor in soup.find_all("a")
        if anchor.get("href") and valid_http_url(anchor.get("href"))
    ]
    plain = soup.get_text("\n", strip=True)
    first_line = next((line for line in plain.split("\n") if line.strip()), "")
    name = _clean_company_name(first_line.split("|")[0])
    if not name:
        return None

    ats = extract_ats(" ".join(links)) or extract_ats(html)
    website = None
    for link in links:
        if safe_domain(link):
            website = link
            break
    if not ats and not website:
        return None

    domain = safe_domain(website)
    comment_id = str(comment.get("id") or "")
    key = f"domain:{domain}" if domain else f"hn:{comment_id}"
    return Candidate(
        company_key=key,
        name=name,
        domain=domain,
        website_url=website,
        ats_type=ats[0] if ats else None,
        ats_slug=ats[1] if ats else None,
        discovery_source="hn_who_is_hiring",
        discovery_url=f"https://news.ycombinator.com/item?id={comment_id}",
        raw={
            "thread_id": thread_id,
            "comment_id": comment_id,
            "text": html,
            "links": links,
        },
    )


def fetch(client: HttpClient, limit: int = 120) -> list[Candidate]:
    thread_id = latest_hiring_thread(client)
    if not thread_id:
        return []
    try:
        data = client.get_json(ITEM_URL.format(id=thread_id))
    except HttpError as exc:
        log.warning("HN thread failed: %s", exc)
        return []
    out: list[Candidate] = []
    seen: set[str] = set()
    for child in data.get("children", []) if isinstance(data, dict) else []:
        if not isinstance(child, dict):
            continue
        candidate = parse_comment(child, thread_id)
        if candidate and candidate.company_key not in seen:
            seen.add(candidate.company_key)
            out.append(candidate)
        if len(out) >= limit:
            break
    return out