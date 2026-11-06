"""Polite HTTP fetcher: robots.txt, per-domain delay, retries, disk cache, optional Playwright."""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .config import Settings

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    content_type: str
    from_cache: bool = False

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class BlockedByRobots(Exception):
    pass


class Fetcher:
    def __init__(self, settings: Settings):
        self.s = settings
        self.client = httpx.Client(
            headers={
                "User-Agent": settings.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-IN,en;q=0.9",
            },
            follow_redirects=True,
            timeout=settings.request_timeout,
        )
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, Optional[RobotFileParser]] = {}
        self._pw = None
        self._browser = None

    # ---------- public ----------

    def get(self, url: str, render: bool = False, use_cache: bool = True) -> FetchResult:
        if use_cache:
            cached = self._cache_read(url, render)
            if cached:
                return cached

        if not self._allowed(url):
            raise BlockedByRobots(url)

        if render and self.s.use_playwright:
            result = self._get_rendered(url)
        else:
            if render:
                log.warning("Source needs JS but USE_PLAYWRIGHT is off; trying static fetch: %s", url)
            result = self._get_static(url)

        if result.ok and use_cache:
            self._cache_write(url, render, result)
        return result

    def close(self) -> None:
        self.client.close()
        if self._browser:
            self._browser.close()
        if self._pw:
            self._pw.stop()

    # ---------- internals ----------

    def _throttle(self, url: str) -> None:
        domain = urlparse(url).netloc
        wait = self.s.request_delay - (time.monotonic() - self._last_hit.get(domain, 0))
        if wait > 0:
            time.sleep(wait)
        self._last_hit[domain] = time.monotonic()

    def _get_static(self, url: str) -> FetchResult:
        last_exc: Optional[Exception] = None
        for attempt in range(3):
            self._throttle(url)
            try:
                r = self.client.get(url)
                if r.status_code in RETRY_STATUSES and attempt < 2:
                    retry_after = r.headers.get("Retry-After", "")
                    backoff = int(retry_after) if retry_after.isdigit() else 5 * (attempt + 1)
                    log.info("HTTP %s for %s, retrying in %ss", r.status_code, url, backoff)
                    time.sleep(min(backoff, 60))
                    continue
                return FetchResult(str(r.url), r.status_code, r.content, r.headers.get("content-type", ""))
            except httpx.HTTPError as e:
                last_exc = e
                log.info("Network error for %s (%s), attempt %d", url, e, attempt + 1)
                time.sleep(5 * (attempt + 1))
        raise last_exc or RuntimeError(f"Failed to fetch {url}")

    def _get_rendered(self, url: str) -> FetchResult:
        if self._browser is None:
            from playwright.sync_api import sync_playwright  # optional dependency

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        self._throttle(url)
        page = self._browser.new_page(user_agent=self.s.user_agent)
        try:
            resp = page.goto(url, wait_until="networkidle", timeout=self.s.request_timeout * 1000)
            html = page.content()
            status = resp.status if resp else 200
            return FetchResult(page.url, status, html.encode("utf-8"), "text/html")
        finally:
            page.close()

    def _allowed(self, url: str) -> bool:
        if not self.s.respect_robots:
            return True
        parsed = urlparse(url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        if base not in self._robots:
            rp: Optional[RobotFileParser] = RobotFileParser()
            try:
                self._throttle(base)
                r = self.client.get(base + "/robots.txt")
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt -> everything allowed
            except httpx.HTTPError:
                rp = None
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.s.user_agent, url)

    def _cache_paths(self, url: str, render: bool):
        key = hashlib.sha1(f"{render}:{url}".encode()).hexdigest()
        return self.s.cache_dir / f"{key}.bin", self.s.cache_dir / f"{key}.json"

    def _cache_read(self, url: str, render: bool) -> Optional[FetchResult]:
        body, meta = self._cache_paths(url, render)
        if not (body.exists() and meta.exists()):
            return None
        if time.time() - meta.stat().st_mtime > self.s.cache_ttl_hours * 3600:
            return None
        m = json.loads(meta.read_text())
        return FetchResult(m["url"], m["status"], body.read_bytes(), m["content_type"], from_cache=True)

    def _cache_write(self, url: str, render: bool, r: FetchResult) -> None:
        body, meta = self._cache_paths(url, render)
        body.write_bytes(r.content)
        meta.write_text(json.dumps({"url": r.url, "status": r.status, "content_type": r.content_type}))
