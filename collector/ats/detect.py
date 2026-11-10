from __future__ import annotations

import logging
import re
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..http import HttpClient, HttpError
from ..models import AtsBoard, Candidate
from ..normalize import domain_label, slugify, valid_http_url
from .adapters import probe
from .patterns import board_url, extract_ats

log = logging.getLogger(__name__)

CAREERS_SUFFIXES = ("/careers", "/jobs", "/company/careers", "/about/careers")
MAX_PAGE_FETCHES = 3
MAX_GUESS_PROBES = 6
GUESS_PROVIDERS = ("greenhouse", "lever", "ashby")


def _walk_urls(value: Any, depth: int = 0) -> Iterable[str]:
    if depth > 4:
        return
    if isinstance(value, str):
        if valid_http_url(value):
            yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk_urls(item, depth + 1)
    elif isinstance(value, list):
        for item in value[:50]:
            yield from _walk_urls(item, depth + 1)


def _careers_urls(candidate: Candidate) -> list[str]:
    urls: list[str] = []
    if candidate.website_url and valid_http_url(candidate.website_url):
        base = candidate.website_url.rstrip("/")
        urls.extend(base + suffix for suffix in CAREERS_SUFFIXES)
        urls.append(base)
    for link in _walk_urls(candidate.raw):
        if valid_http_url(link) and not extract_ats(link):
            urls.append(link)
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        normalized = url.rstrip("/")
        if normalized not in seen:
            seen.add(normalized)
            result.append(url)
    return result


def _board_from_match(
    provider: str,
    token: str,
    *,
    confidence: float,
    detected_via: str,
    url: str = "",
    name: str | None = None,
) -> AtsBoard:
    region = "eu" if "jobs.eu.lever.co" in (url or "") or "api.eu.lever.co" in (url or "") else "global"
    board_page, api = board_url(provider, token, region)
    return AtsBoard(
        provider=provider,
        token=token,
        board_url=board_page,
        api_url=api,
        confidence=confidence,
        detected_via=detected_via,
        region=region,
        name=name,
    )


def _validated_board(
    client: HttpClient,
    provider: str,
    token: str,
    *,
    confidence: float,
    detected_via: str,
    url: str = "",
) -> AtsBoard | None:
    result = probe(client, provider, token)
    if not result.ok:
        log.debug("ATS probe rejected %s/%s: %s", provider, token, result.error_code)
        return None
    return _board_from_match(
        provider,
        token,
        confidence=confidence,
        detected_via=detected_via,
        url=url,
        name=result.name,
    )


def detect_ats(client: HttpClient, candidate: Candidate) -> AtsBoard | None:
    if candidate.ats_type and candidate.ats_slug:
        board = _validated_board(
            client,
            candidate.ats_type,
            candidate.ats_slug,
            confidence=0.9,
            detected_via="discovery",
            url=candidate.discovery_url or "",
        )
        if board:
            return board

    # 1. Look for an explicit ATS link in the discovery payload.
    for link in _walk_urls(candidate.raw):
        match = extract_ats(link)
        if match:
            board = _validated_board(
                client,
                match[0],
                match[1],
                confidence=0.95,
                detected_via="discovery_link",
                url=link,
            )
            if board:
                return board

    # 2. Fetch the company's careers page and inspect the final URL and HTML.
    for page_url in _careers_urls(candidate)[:MAX_PAGE_FETCHES]:
        try:
            response = client.get_text(page_url, allow_404=True)
        except HttpError as exc:
            log.debug("careers page failed %s: %s", page_url, exc.code)
            continue
        if response is None:
            continue
        match = extract_ats(response.url) or extract_ats(response.text)
        if not match:
            soup = BeautifulSoup(response.text, "html.parser")
            for iframe in soup.find_all("iframe"):
                src = iframe.get("src")
                if not src:
                    continue
                absolute = urljoin(response.url, str(src))
                match = extract_ats(absolute)
                if match:
                    page_url = absolute
                    break
        if match:
            board = _validated_board(
                client,
                match[0],
                match[1],
                confidence=0.95,
                detected_via="careers_page",
                url=page_url,
            )
            if board:
                return board

    # 3. Last resort: try a small set of slug guesses. This is deliberately
    # bounded so a bad guess cannot burn the run budget.
    bases: list[str] = []
    if candidate.name:
        bases.append(slugify(candidate.name))
    if candidate.domain:
        bases.append(domain_label(candidate.domain))
    if candidate.ats_slug:
        bases.append(slugify(candidate.ats_slug))
    variants: list[str] = []
    for base in bases:
        if not base:
            continue
        for value in (base, f"{base}hq", f"{base}careers", base.replace("-", "")):
            if value and value not in variants:
                variants.append(value)

    probes = 0
    for provider in GUESS_PROVIDERS:
        for token in variants:
            if probes >= MAX_GUESS_PROBES:
                break
            probes += 1
            board = _validated_board(
                client,
                provider,
                token,
                confidence=0.55,
                detected_via="slug_guess",
            )
            if board:
                return board
        if probes >= MAX_GUESS_PROBES:
            break
    return None