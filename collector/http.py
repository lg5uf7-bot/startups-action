from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests

from .config import Settings

log = logging.getLogger(__name__)


class HttpError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str = "http_error",
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.url = url


@dataclass
class TextResponse:
    text: str
    url: str
    status: int


class HttpClient:
    """Small, polite HTTP client with per-host spacing and bounded retries."""

    RETRY_STATUS = {429, 500, 502, 503, 504}

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": settings.user_agent,
                "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
                "Accept-Language": "en-US,en;q=0.8",
            }
        )
        self._last_request: dict[str, float] = {}

    def _wait(self, url: str, delay: float | None = None) -> None:
        host = urlsplit(url).hostname or ""
        wait = (self.settings.http_delay if delay is None else delay) - (
            time.monotonic() - self._last_request.get(host, -10_000.0)
        )
        if wait > 0:
            time.sleep(wait)

    def _request(
        self,
        method: str,
        url: str,
        *,
        expect_json: bool,
        delay: float | None = None,
        timeout: int = 45,
        allow_404: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Any:
        last_error: Exception | None = None
        for attempt in range(3):
            self._wait(url, delay=delay)
            try:
                response = self.session.request(
                    method,
                    url,
                    timeout=timeout,
                    allow_redirects=True,
                    headers=headers,
                )
                self._last_request[urlsplit(url).hostname or ""] = time.monotonic()
                status = response.status_code
                if status == 404 and allow_404:
                    return None
                if status in self.RETRY_STATUS and attempt < 2:
                    retry_after = response.headers.get("Retry-After")
                    sleep_for = 2 ** (attempt + 1)
                    if retry_after:
                        try:
                            sleep_for = max(sleep_for, float(retry_after))
                        except ValueError:
                            pass
                    sleep_for += random.uniform(0, 0.75)
                    log.warning("HTTP %s %s, retrying in %.1fs", status, url, sleep_for)
                    time.sleep(min(sleep_for, 45))
                    continue
                if status == 429:
                    raise HttpError(
                        "rate limited", status=status, code="rate_limited", url=url
                    )
                if status >= 500:
                    raise HttpError(
                        f"server error {status}",
                        status=status,
                        code="server_error",
                        url=url,
                    )
                if status == 404:
                    raise HttpError("not found", status=status, code="not_found", url=url)
                if status == 403:
                    raise HttpError("forbidden", status=status, code="forbidden", url=url)
                response.raise_for_status()
                if expect_json:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise HttpError(
                            "invalid JSON",
                            status=status,
                            code="invalid_json",
                            url=url,
                        ) from exc
                return TextResponse(text=response.text, url=response.url, status=status)
            except HttpError:
                raise
            except requests.Timeout as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1) + random.uniform(0, 0.5))
                    continue
                raise HttpError("timeout", code="timeout", url=url) from exc
            except requests.RequestException as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(2 ** (attempt + 1) + random.uniform(0, 0.5))
                    continue
                raise HttpError(str(exc)[:240], code="network_error", url=url) from exc
        raise HttpError(str(last_error or "request failed"), code="network_error", url=url)

    def get_json(
        self,
        url: str,
        *,
        delay: float | None = None,
        allow_404: bool = False,
        headers: dict[str, str] | None = None,
    ) -> Any:
        return self._request(
            "GET",
            url,
            expect_json=True,
            delay=delay,
            allow_404=allow_404,
            headers=headers,
        )

    def get_text(
        self,
        url: str,
        *,
        delay: float | None = None,
        allow_404: bool = False,
        headers: dict[str, str] | None = None,
    ) -> TextResponse:
        return self._request(
            "GET",
            url,
            expect_json=False,
            delay=delay,
            allow_404=allow_404,
            headers=headers,
        )