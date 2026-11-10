from __future__ import annotations

import re
from urllib.parse import quote

# Ordered from most specific to least specific. The token is the first capture
# group. Greenhouse has two public hostnames and Lever has a regional host.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "greenhouse",
        re.compile(
            r"(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
    ),
    (
        "lever",
        re.compile(
            r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
    ),
    (
        "ashby",
        re.compile(
            r"jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)",
            re.IGNORECASE,
        ),
    ),
    (
        "workable",
        re.compile(
            r"(?:apply\.workable\.com|jobs\.workable\.com)/(?:api/v[0-9]+/widget/accounts/)?([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
    ),
    (
        "smartrecruiters",
        re.compile(
            r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
    ),
]

PROVIDERS = {name for name, _ in PATTERNS}


def extract_ats(value: object) -> tuple[str, str] | None:
    text = str(value or "")
    if not text:
        return None
    for provider, pattern in PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        token = (match.group(1) or "").strip().strip("/")
        token = token.split("?")[0].split("#")[0]
        if token and token.lower() not in {"embed", "job_board", "jobs"}:
            return provider, token
    return None


def board_url(provider: str, token: str, region: str = "global") -> tuple[str, str]:
    token = quote(str(token or "").strip(), safe="")
    provider = str(provider or "").strip().lower()
    if provider == "greenhouse":
        return (
            f"https://job-boards.greenhouse.io/{token}",
            f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true",
        )
    if provider == "lever":
        host = "jobs.eu.lever.co" if region == "eu" else "jobs.lever.co"
        api_host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
        return (
            f"https://{host}/{token}",
            f"https://{api_host}/v0/postings/{token}?mode=json",
        )
    if provider == "ashby":
        return (
            f"https://jobs.ashbyhq.com/{token}",
            f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true",
        )
    if provider == "workable":
        return (
            f"https://apply.workable.com/{token}/",
            f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true",
        )
    if provider == "smartrecruiters":
        return (
            f"https://jobs.smartrecruiters.com/{token}",
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=100",
        )
    raise ValueError(f"Unsupported ATS provider: {provider}")