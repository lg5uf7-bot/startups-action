"""Bounded HTTP JSON client. One request per call, no hidden retries or HTML pages."""
from __future__ import annotations
import gzip, hashlib, json, time
from pathlib import Path
from urllib.parse import urlsplit
import requests

class FetchError(Exception):
    pass

class JsonClient:
    def __init__(self, delay=1.05, timeout=45):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent':'StartupDailySimulation/1.0 (public JSON, respectful rate)', 'Accept':'application/json'})
        self.delay = delay
        self.timeout = timeout
        self.last = {}
        self.blocked_hosts = set()
        self.events = []
        self.cache = {}

    def get(self, url, kind, snapshot=None, cache=False):
        if cache and url in self.cache:
            return self.cache[url]
        host = urlsplit(url).hostname
        if host in self.blocked_hosts:
            raise FetchError(f'{host}: circuit open after HTTP 429')
        wait = self.delay - (time.monotonic() - self.last.get(host, -1e12))
        if wait > 0:
            time.sleep(wait)
        start = time.monotonic()
        ev = {'kind':kind, 'url':url, 'status':'error'}
        try:
            response = self.session.get(url, timeout=self.timeout, allow_redirects=True)
            self.last[host] = time.monotonic()
            ev.update(status=response.status_code, elapsed_ms=round((time.monotonic()-start)*1000), bytes=len(response.content))
            if response.status_code == 429:
                self.blocked_hosts.add(host)
            response.raise_for_status()
            # Reject non-JSON HTTP 200 rather than interpreting an HTML error as empty jobs.
            obj = response.json()
            data = response.content
            ev['sha256'] = hashlib.sha256(data).hexdigest()
            if snapshot:
                path = Path(snapshot)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(gzip.compress(data, compresslevel=7, mtime=0))
            if cache:
                self.cache[url] = obj
            return obj
        except (requests.RequestException, ValueError) as exc:
            self.last[host] = time.monotonic()
            ev['error'] = str(exc)[:240]
            raise FetchError(str(exc)[:240]) from exc
        finally:
            self.events.append(ev)
