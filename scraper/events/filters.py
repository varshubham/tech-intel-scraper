"""Keep only upcoming, tech-related events."""
from __future__ import annotations

import re
from datetime import date
from functools import lru_cache


@lru_cache(maxsize=8)
def _compile(words: tuple) -> re.Pattern:
    """Short all-caps acronyms (IT, AI, AR, EV) match case-sensitively so 'IT' doesn't hit the word 'it';
    everything else is case-insensitive."""
    parts = []
    for w in words:
        w = str(w)
        if w.isupper() and len(w) <= 4:
            parts.append(re.escape(w))
        else:
            parts.append("(?i:" + re.escape(w) + ")")
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b")


def is_tech(ev: dict, keywords: dict) -> bool:
    include = tuple(keywords.get("tech", []))
    exclude = tuple(keywords.get("exclude", []))
    text = " ".join(str(ev.get(k) or "") for k in ("title", "description", "category", "organizer"))
    if exclude and _compile(exclude).search(ev.get("title") or ""):
        return False
    return bool(include) and bool(_compile(include).search(text))


def is_upcoming(ev: dict, keep_undated: bool = False) -> bool:
    last_day = ev.get("end_date") or ev.get("start_date")
    if not last_day:
        return keep_undated
    return last_day >= date.today().isoformat()


def title_excluded(ev: dict, pattern: str) -> bool:
    """Per-source `exclude_titles` regex, for listing cards that aren't events (CFPs, sponsor pages)."""
    return bool(pattern) and re.search(pattern, ev.get("title") or "", re.I) is not None


COUNTRY_ALIASES ={"in": "in", "ind": "in", "india": "in", "bharat": "in"}


def in_country(ev: dict, country: str) -> bool:
    """False only when the event names a different country; events without one are kept."""
    if not country or not ev.get("country"):
        return True
    return _country_code(ev["country"]) == _country_code(country)


def _country_code(c: str) -> str:
    c = c.strip().lower()
    return COUNTRY_ALIASES.get(c, c)


PLACEHOLDERS ={"n/a", "na", "-", "--", "tba", "tbd", "null", "none"}


def clean(ev: dict) -> dict:
    for k, v in list(ev.items()):
        if isinstance(v, str):
            v = " ".join(v.split())
            ev[k] = None if not v or v.lower() in PLACEHOLDERS else v
    if ev.get("city"):
        ev["city"] = ev["city"].strip(" ,;-") or None
    if ev.get("description") and len(ev["description"]) > 2000:
        ev["description"] = ev["description"][:2000] + "…"
    if ev.get("end_date") and ev.get("start_date") and ev["end_date"] < ev["start_date"]:
        ev["end_date"] = None
    return ev
