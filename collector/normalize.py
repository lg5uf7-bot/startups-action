from __future__ import annotations

import hashlib
import html as html_lib
import json
import re
import unicodedata
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tldextract
from bs4 import BeautifulSoup
from dateutil import parser as date_parser

EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), include_psl_private_domains=True)

# Domains that are never a company's own website. They are useful as job or
# social links, but not as the canonical company domain.
BLOCKED_DOMAINS = {
    "linkedin.com",
    "facebook.com",
    "twitter.com",
    "x.com",
    "github.com",
    "crunchbase.com",
    "instagram.com",
    "youtube.com",
    "linktr.ee",
    "medium.com",
    "notion.so",
    "google.com",
    "wellfound.com",
    "wikipedia.org",
    "greenhouse.io",
    "ashbyhq.com",
    "lever.co",
    "workable.com",
    "smartrecruiters.com",
    "breezy.hr",
    "recruitee.com",
    "teamtailor.com",
    "remoteok.com",
    "remotive.com",
    "weworkremotely.com",
    "ycombinator.com",
    "news.ycombinator.com",
}

REMOTE_RE = re.compile(
    r"\b(remote|anywhere|distributed|work\s*from\s*home|wfh|worldwide|global)\b",
    re.IGNORECASE,
)
MONEY_RE = re.compile(
    r"(?P<currency>USD|EUR|GBP|INR|PHP|CAD|AUD|\$|€|£|¥)?\s*"
    r"(?P<amount>\d[\d,.]*)\s*(?P<suffix>[KMB])?",
    re.IGNORECASE,
)


def valid_http_url(value: object) -> bool:
    try:
        parts = urlsplit(str(value or "").strip())
        return (
            parts.scheme in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        return False


def host_of(url: object) -> str | None:
    if not valid_http_url(url):
        return None
    host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
    return host or None


def safe_domain(url: object) -> str | None:
    """Return the registrable company domain, or None for blocked/shared hosts."""
    if not valid_http_url(url):
        return None
    host = host_of(url)
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    parsed = EXTRACT(host)
    if not parsed.suffix or not parsed.domain:
        return None
    domain = f"{parsed.domain}.{parsed.suffix}".lower()
    if domain in BLOCKED_DOMAINS:
        return None
    return domain


def domain_label(domain: str | None) -> str:
    if not domain:
        return ""
    return domain.split(".")[0]


def name_key(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return "".join(ch for ch in text if ch.isalnum())


def slugify(value: object, fallback: str = "company") -> str:
    text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return (text[:80] or fallback).strip("-") or fallback


def normalized_url(url: object) -> str:
    if not valid_http_url(url):
        return ""
    parts = urlsplit(str(url).strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() != "gh_src"
    ]
    host = (parts.hostname or "").lower()
    if parts.port and parts.port not in (80, 443):
        host = f"{host}:{parts.port}"
    return urlunsplit(
        (
            parts.scheme.lower(),
            host,
            parts.path.rstrip("/") or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def strip_html(value: object, max_chars: int = 20000) -> str:
    text = str(value or "")
    if not text:
        return ""
    if "<" in text and ">" in text:
        soup = BeautifulSoup(text, "html.parser")
        text = soup.get_text(" ", strip=True)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def truncate(value: object, max_chars: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:max_chars]


def first_nonempty(*values: object) -> object | None:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        return value
    return None


def as_list(value: object) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def unique_strings(values: object) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in as_list(values):
        text = str(value or "").strip()
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            result.append(text)
    return result


def parse_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 10_000_000_000:
            number /= 1000.0
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{4}", text):
        return datetime(int(text), 1, 1, tzinfo=timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}", text):
        year, month = text.split("-")
        return datetime(int(year), int(month), 1, tzinfo=timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        year, month, day = text.split("-")
        return datetime(int(year), int(month), int(day), tzinfo=timezone.utc)
    try:
        parsed = date_parser.parse(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        try:
            parsed = parsedate_to_datetime(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError, OverflowError):
            return None


def parse_date(value: object) -> date | None:
    parsed = parse_datetime(value)
    return parsed.date() if parsed else None


def date_precision(value: object) -> str | None:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}", text):
        return "year"
    if re.fullmatch(r"\d{4}-\d{2}", text):
        return "month"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return "day"
    return "unknown" if text else None


def parse_money(value: object) -> tuple[float | None, str | None]:
    text = str(value or "").strip()
    if not text:
        return None, None
    match = MONEY_RE.search(text.replace(",", ""))
    if not match:
        return None, None
    amount = float(match.group("amount"))
    suffix = (match.group("suffix") or "").upper()
    if suffix == "K":
        amount *= 1_000
    elif suffix == "M":
        amount *= 1_000_000
    elif suffix == "B":
        amount *= 1_000_000_000
    currency = (match.group("currency") or "").upper()
    currency_map = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"}
    currency = currency_map.get(currency, currency or None)
    return amount, currency


def infer_remote(
    location: object,
    workplace_type: object = None,
    is_remote: object = None,
) -> bool | None:
    if isinstance(is_remote, bool):
        return is_remote
    workplace = str(workplace_type or "").strip().lower()
    if workplace in {"remote", "fully remote"}:
        return True
    if workplace in {"hybrid"}:
        return False
    if workplace in {"on-site", "onsite", "office"}:
        return False
    text = str(location or "")
    if not text:
        return None
    if REMOTE_RE.search(text):
        return True
    return False


def normalize_employment_type(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    text = re.sub(r"[\s_-]+", "", text)
    if text in {"fulltime", "fulltimeemployee", "permanent"}:
        return "full_time"
    if text in {"parttime"}:
        return "part_time"
    if text in {"contract", "contractor", "freelance", "temporary", "temp"}:
        return "contract"
    if text in {"intern", "internship"}:
        return "internship"
    if text in {"volunteer"}:
        return "volunteer"
    if text in {"other", "none", "unknown"}:
        return None
    return text[:40]


def content_hash(*parts: object) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part or "").encode("utf-8", "ignore"))
        digest.update(b"\x00")
    return digest.hexdigest()


def json_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def clean_text(value: object, max_chars: int = 20000) -> str | None:
    text = strip_html(value, max_chars=max_chars)
    return text or None


def is_probably_company_name(value: object) -> bool:
    text = str(value or "").strip()
    if len(text) < 2 or len(text) > 120:
        return False
    lowered = text.lower()
    return not any(
        bad in lowered
        for bad in (
            "http://",
            "https://",
            "we are hiring",
            "apply now",
            "job description",
        )
    )