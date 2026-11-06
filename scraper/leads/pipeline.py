"""Leads run: businesses around a point -> their websites -> contacts -> DB."""
from __future__ import annotations

import logging
from typing import Optional

from ..core.config import Settings
from ..core.db import DB
from ..core.fetcher import Fetcher
from .contacts import collect_contacts
from .places import find_businesses

log = logging.getLogger(__name__)


def run_leads(settings: Settings, lat: float, lng: float, radius_km: float, types: list[str],
              contacts: bool = True, db: Optional[DB] = None) -> list[dict]:
    """Find businesses, visit their websites for contacts, save to `db` if given. Returns the businesses."""
    businesses = find_businesses(settings, lat, lng, radius_km, types)
    log.info("Found %d businesses within %s km (%s)", len(businesses), radius_km, ", ".join(types))
    if len(businesses) > settings.lead_max_businesses:
        log.info("Keeping the nearest %d (LEAD_MAX_BUSINESSES)", settings.lead_max_businesses)
        businesses = businesses[: settings.lead_max_businesses]

    fetcher = Fetcher(settings) if contacts else None
    try:
        for i, b in enumerate(businesses, 1):
            b.setdefault("phones", [])
            b.setdefault("people", [])
            b.setdefault("contact_pages", [])
            if fetcher and b.get("website"):
                log.info("[%d/%d] %s -> %s", i, len(businesses), b["name"], b["website"])
                found = collect_contacts(fetcher, settings, b["website"])
                b["emails"] = list(dict.fromkeys(b["emails"] + found["emails"]))
                b["phones"] = found["phones"]
                b["people"] = found["people"]
                b["contact_pages"] = found["contact_pages"]
            if db:
                db.upsert_business(b)
    finally:
        if fetcher:
            fetcher.close()
    return businesses
