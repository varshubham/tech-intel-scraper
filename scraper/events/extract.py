"""Turn an HTML page into a list of event dicts.

Layers, most reliable first:
  1. schema.org Event JSON-LD embedded in the page (many event sites ship this)
  2. CSS selectors declared per source in sources.yaml
  3. (optional) local LLM via Ollama — see llm.py
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from .dates import parse_date, parse_date_range

log = logging.getLogger(__name__)

BROCHURE_HINT = re.compile(r"brochure|prospectus|agenda|download|leaflet|flyer|pamphlet|invitation", re.I)


def soupify(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


# ---------------- JSON-LD ----------------

def _walk_jsonld(node) -> Iterable[dict]:
    if isinstance(node, list):
        for n in node:
            yield from _walk_jsonld(n)
    elif isinstance(node, dict):
        yield node
        for key in ("@graph", "itemListElement", "item", "subEvent", "event", "events"):
            if key in node:
                yield from _walk_jsonld(node[key])


def _is_event(node: dict) -> bool:
    t = node.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.endswith("Event") for x in types)


def _text(v) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, list):
        v = v[0] if v else None
    if isinstance(v, dict):
        v = v.get("name") or v.get("@id")
    if v is None:
        return None
    return " ".join(str(v).split()) or None


def _location(loc) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """-> (venue, city, mode)"""
    if isinstance(loc, list):
        parsed = [_location(l) for l in loc]
        modes = {p[2] for p in parsed if p[2]}
        venue = next((p[0] for p in parsed if p[0]), None)
        city = next((p[1] for p in parsed if p[1]), None)
        return venue, city, "hybrid" if len(modes) > 1 else (modes.pop() if modes else None)
    if isinstance(loc, str):
        return loc, None, None
    if not isinstance(loc, dict):
        return None, None, None
    if "VirtualLocation" in str(loc.get("@type", "")):
        return None, None, "online"
    addr = loc.get("address")
    city = None
    if isinstance(addr, dict):
        city = _text(addr.get("addressLocality")) or _text(addr.get("addressRegion"))
    elif isinstance(addr, str):
        city = addr.split(",")[-2].strip() if addr.count(",") >= 2 else None
    return _text(loc.get("name")), city, "offline"


def _country(loc) -> Optional[str]:
    if isinstance(loc, list):
        return next((c for c in map(_country, loc) if c), None)
    addr = loc.get("address") if isinstance(loc, dict) else None
    if not isinstance(addr, dict):
        return None
    c = addr.get("addressCountry")
    return _text(c.get("name") if isinstance(c, dict) else c)


def _mode_from_attendance(v) -> Optional[str]:
    s = str(v or "")
    if "Mixed" in s:
        return "hybrid"
    if "Online" in s:
        return "online"
    if "Offline" in s:
        return "offline"
    return None


def _price(offers) -> Optional[str]:
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return None
    price = offers.get("price") or offers.get("lowPrice")
    if price in (None, ""):
        return None
    if str(price) in ("0", "0.0", "0.00"):
        return "Free"
    return f"{offers.get('priceCurrency', '')} {price}".strip()


def extract_jsonld(soup: BeautifulSoup, page_url: str) -> list[dict]:
    events = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw.strip())
        except ValueError:
            # some sites put several objects or trailing commas; try a lenient pass
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw.strip()))
            except ValueError:
                continue
        for node in _walk_jsonld(data):
            if not _is_event(node) or not node.get("name"):
                continue
            venue, city, mode = _location(node.get("location"))
            organizer = node.get("organizer")
            events.append({
                "title": _text(node.get("name")),
                "start_date": parse_date(node.get("startDate")),
                "end_date": parse_date(node.get("endDate")),
                "venue": venue,
                "city": city,
                "country": _country(node.get("location")),
                "mode": _mode_from_attendance(node.get("eventAttendanceMode")) or mode,
                "organizer": _text(organizer),
                "description": _text(node.get("description")),
                "price": _price(node.get("offers")),
                "registration_url": urljoin(page_url, node["url"]) if isinstance(node.get("url"), str) else None,
                "category": _text(node.get("@type")),
            })
    return events


# ---------------- CSS selectors ----------------

def _select_value(el: Tag, spec: str, page_url: str) -> Optional[str]:
    """spec examples: 'h3', '.date', 'a@href', 'img@src', 'time@datetime'."""
    sel, _, attr = spec.partition("@")
    target = el.select_one(sel) if sel else el
    if target is None:
        return None
    if attr:
        val = target.get(attr)
        if val and attr in ("href", "src"):
            val = urljoin(page_url, val)
        return val
    return " ".join(target.get_text(" ").split()) or None


def extract_with_selectors(soup: BeautifulSoup, page_url: str, cfg: dict) -> list[dict]:
    fields = cfg.get("fields", {})
    events = []
    for item in soup.select(cfg["item"]):
        raw = {name: _select_value(item, spec, page_url) for name, spec in fields.items()}
        if not raw.get("title"):
            continue
        start, end = parse_date_range(raw.get("date") or "")
        if raw.get("start_date"):
            start = parse_date(raw["start_date"]) or start
        if raw.get("end_date"):
            end = parse_date(raw["end_date"]) or end
        events.append({
            "title": raw["title"],
            "start_date": start,
            "end_date": end,
            "venue": raw.get("venue") or raw.get("location"),
            "city": raw.get("city"),
            "mode": raw.get("mode"),
            "organizer": raw.get("organizer"),
            "description": raw.get("description"),
            "price": raw.get("price"),
            "registration_url": raw.get("link"),
            "category": raw.get("category"),
            "brochure": raw.get("brochure"),
        })
    return events


# ---------------- links ----------------

def find_brochure_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    links = []
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        path = href.split("?")[0].split("#")[0].lower()
        text = " ".join(a.get_text(" ").split()) + " " + (a.get("title") or "")
        if path.endswith(".pdf") or (BROCHURE_HINT.search(text) and not path.endswith((".jpg", ".png", ".zip"))):
            if href not in links and href.startswith("http"):
                links.append(href)
    return links


def visible_text(soup: BeautifulSoup, limit: int = 12000) -> str:
    for tag in soup(["script", "style", "noscript", "svg", "header", "footer", "nav"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())[:limit]
