"""Crawl every configured source: listing page(s) -> events -> detail pages -> brochures -> DB."""
from __future__ import annotations

import logging
import re
import threading
from typing import Optional
from urllib.parse import urljoin

from ..core.config import Settings, load_sources
from ..core.db import DB
from ..core.fetcher import BlockedByRobots, Fetcher
from . import brochures, extract, filters
from .llm import extract_with_llm

log = logging.getLogger(__name__)

# Only one crawl at a time (scheduler + manual trigger share this)
crawl_lock = threading.Lock()


def last_page_urls(fetcher: Fetcher, src: dict, render: bool) -> list[str]:
    """For listings sorted oldest-first: read the pagination on `url`, return the last `last_pages` pages."""
    r = fetcher.get(src["url"], render=render)
    if not r.ok:
        return [src["url"]]
    template = src["page_url"]
    pattern = re.compile(re.escape(template).replace(r"\{page\}", r"(\d+)") + "$")
    soup = extract.soupify(r.text)
    pages = [int(m.group(1)) for a in soup.select("a[href]")
             if (m := pattern.match(urljoin(r.url, a["href"])))]
    if not pages:
        return [src["url"]]
    last = max(pages)
    first = max(1, last - int(src["last_pages"]) + 1)
    return [template.format(page=n) for n in range(first, last + 1)]


def extract_events(fetcher: Fetcher, settings: Settings, src: dict) -> list[dict]:
    """Fetch the listing page(s) of one source and return raw (unfiltered) events."""
    render = src.get("type") == "js"
    if src.get("last_pages"):
        urls = last_page_urls(fetcher, src, render)
    else:
        urls = src.get("urls") or [src["url"]]
    events: list[dict] = []
    errors: list[str] = []
    for url in urls:
        try:
            r = fetcher.get(url, render=render)
        except Exception as e:  # robots block, network failure
            errors.append(f"{url}: {e!r}")
            continue
        if not r.ok:
            errors.append(f"HTTP {r.status} for {url}")
            log.warning("[%s] HTTP %s for %s", src["name"], r.status, url)
            continue
        soup = extract.soupify(r.text)

        found = extract.extract_jsonld(soup, r.url)
        if src.get("selectors"):
            found += extract.extract_with_selectors(soup, r.url, src["selectors"])
        if not found and settings.use_llm:
            log.info("[%s] rules found nothing, trying LLM", src["name"])
            found = extract_with_llm(settings, extract.visible_text(soup), r.url)

        for ev in found:
            ev["source_url"] = r.url
            ev.setdefault("organizer", None)
            if not ev.get("organizer") and src.get("organizer"):
                ev["organizer"] = src["organizer"]
        log.info("[%s] %s -> %d raw events", src["name"], url, len(found))
        events += found
    if errors and len(errors) == len(urls):
        raise RuntimeError("; ".join(errors)[:500])
    return events


def enrich_from_detail(fetcher: Fetcher, src: dict, ev: dict) -> list[str]:
    """Open the event's own page: fill missing fields from its JSON-LD and collect brochure links."""
    url = ev.get("registration_url")
    if not url:
        return []
    try:
        r = fetcher.get(url, render=src.get("type") == "js")
    except BlockedByRobots:
        log.info("robots.txt disallows %s", url)
        return []
    except Exception as e:
        log.info("Detail page failed %s: %s", url, e)
        return []
    if not r.ok or "html" not in r.content_type.lower():
        return []
    soup = extract.soupify(r.text)
    detail = extract.extract_jsonld(soup, r.url)
    if detail:
        best = detail[0]
        for k, v in best.items():
            if v and not ev.get(k):
                ev[k] = v
    return extract.find_brochure_links(soup, r.url)


def crawl_source(fetcher: Fetcher, db: DB, settings: Settings, src: dict) -> tuple[int, int]:
    raw = extract_events(fetcher, settings, src)
    tech_only = src.get("tech_only", False)
    kept = []
    for ev in raw:
        ev = filters.clean(ev)
        if not ev.get("title"):
            continue
        if not tech_only and not filters.is_tech(ev, settings.keywords):
            continue
        if not filters.in_country(ev, src.get("country", "")):
            continue
        if filters.title_excluded(ev, src.get("exclude_titles", "")):
            continue
        kept.append(ev)

    found = new = 0
    detail_budget = src.get("max_detail_pages", settings.max_detail_pages)
    for ev in kept:
        brochure_links = [ev.pop("brochure")] if ev.get("brochure") else []
        if src.get("follow_links", True) and detail_budget > 0 and ev.get("registration_url"):
            detail_budget -= 1
            brochure_links += enrich_from_detail(fetcher, src, ev)
            ev = filters.clean(ev)

        if not filters.is_upcoming(ev, keep_undated=src.get("keep_undated", False)):
            continue

        event_id, is_new = db.upsert_event(ev, src["name"])
        found += 1
        new += int(is_new)

        if settings.download_brochures:
            for link in dict.fromkeys(brochure_links):  # dedupe, keep order
                if db.has_brochure(event_id, link):
                    continue
                info = brochures.download_brochure(fetcher, settings, link)
                if info:
                    db.add_brochure(event_id, info)
                    log.info("[%s] saved brochure %s", src["name"], link)
    return found, new


def run_crawl(settings: Settings, only: Optional[str] = None) -> list[dict]:
    """Crawl all enabled sources (or just one). Returns a summary per source."""
    if not crawl_lock.acquire(blocking=False):
        log.warning("A crawl is already running; skipping")
        return [{"status": "skipped", "reason": "crawl already running"}]
    try:
        db = DB(settings.db_path)
        fetcher = Fetcher(settings)
        summary = []
        try:
            for src in load_sources(settings):
                if only and src["name"] != only:
                    continue
                run_id = db.start_run(src["name"])
                try:
                    found, new = crawl_source(fetcher, db, settings, src)
                    db.finish_run(run_id, "ok", found, new)
                    summary.append({"source": src["name"], "status": "ok", "found": found, "new": new})
                    log.info("[%s] done: %d upcoming tech events (%d new)", src["name"], found, new)
                except Exception as e:  # one broken site must not stop the rest
                    log.exception("[%s] failed", src["name"])
                    db.finish_run(run_id, "error", error=str(e)[:500])
                    summary.append({"source": src["name"], "status": "error", "error": str(e)[:200]})
        finally:
            fetcher.close()
        return summary
    finally:
        crawl_lock.release()
