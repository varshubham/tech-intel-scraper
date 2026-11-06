"""Find businesses of given types around a point: Google Places if a key is set, else OpenStreetMap."""
from __future__ import annotations

import logging
import math
import time
from typing import Optional

import httpx

from ..core.config import Settings

log = logging.getLogger(__name__)

# Public Overpass servers, tried in order until one answers
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
# overpass-api.de answers 406 to browser-like (Mozilla/...) or missing User-Agents
OVERPASS_UA = "tech-intel-scraper/1.0 (business leads; python-httpx)"
OVERPASS_ROUNDS = 3  # the public servers are often busy (502/504); go round the list this many times
PLACES_URL = "https://places.googleapis.com/v1/places:searchNearby"
PLACES_FIELDS = ",".join("places." + f for f in (
    "id", "displayName", "formattedAddress", "location", "primaryType",
    "nationalPhoneNumber", "internationalPhoneNumber", "websiteUri",
))
PLACES_MAX = 20       # searchNearby never returns more than 20 places per call
PLACES_SPLIT_DEPTH = 2  # a full page splits the circle into 4 smaller ones, up to this many times

# LEAD_TYPES names -> OpenStreetMap tags. A type containing "=" is used as a raw tag (e.g. office=it).
OSM_TAGS = {
    "hospital": ["amenity=hospital"],
    "clinic": ["amenity=clinic", "amenity=doctors"],
    "doctor": ["amenity=doctors"],
    "dentist": ["amenity=dentist"],
    "pharmacy": ["amenity=pharmacy"],
    "laboratory": ["healthcare=laboratory"],
    "school": ["amenity=school"],
    "college": ["amenity=college"],
    "university": ["amenity=university"],
    "hotel": ["tourism=hotel"],
    "restaurant": ["amenity=restaurant"],
    "bank": ["amenity=bank"],
    "company": ["office=company"],
    "factory": ["man_made=works", "industrial=factory"],
}
# LEAD_TYPES names -> Google place types, where they differ. Others are sent as-is
# (full list: https://developers.google.com/maps/documentation/places/web-service/place-types).
GOOGLE_TYPES = {
    "clinic": ["doctor"],
    "dentist": ["dentist", "dental_clinic"],
    "laboratory": ["medical_lab"],
    "college": ["university"],
}


def distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def find_businesses(settings: Settings, lat: float, lng: float, radius_km: float, types: list[str]) -> list[dict]:
    """Businesses within radius_km of (lat, lng), nearest first."""
    radius_m = int(radius_km * 1000)
    if settings.google_places_api_key:
        found = _google(settings, lat, lng, radius_m, types)
    else:
        found = _osm(settings, lat, lng, radius_m, types)
    for b in found:
        b["distance_km"] = round(distance_km(lat, lng, b["lat"], b["lng"]), 2)
        b["website"] = _normalise_url(b.get("website"))
    found = [b for b in found if b["distance_km"] <= radius_km]
    return sorted(found, key=lambda b: b["distance_km"])


def _normalise_url(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    url = url.strip().split(";")[0].strip()
    return url if url.startswith(("http://", "https://")) else "https://" + url


# ---------------- OpenStreetMap (free, no key) ----------------

def _osm(settings: Settings, lat: float, lng: float, radius_m: int, types: list[str]) -> list[dict]:
    tag_type: dict[tuple[str, str], str] = {}
    for t in types:
        tags = OSM_TAGS.get(t) or ([t] if "=" in t else [])
        if not tags:
            log.warning("Unknown business type %r for OpenStreetMap; use one of %s or a raw tag like office=it",
                        t, ", ".join(OSM_TAGS))
        for tag in tags:
            k, _, v = tag.partition("=")
            tag_type[(k, v)] = t
    if not tag_type:
        return []
    parts = "".join(f'nwr["{k}"="{v}"](around:{radius_m},{lat},{lng});' for k, v in tag_type)
    query = f"[out:json][timeout:90];({parts});out center tags;"
    last_exc: Optional[Exception] = None
    for attempt in range(OVERPASS_ROUNDS):
        if attempt:
            time.sleep(15 * attempt)
        for url in OVERPASS_URLS:
            try:
                r = httpx.post(url, data={"data": query}, headers={"User-Agent": OVERPASS_UA}, timeout=120)
                r.raise_for_status()
                return parse_osm(r.json().get("elements", []), tag_type)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("Overpass server %s failed (%s), trying the next one", url, str(e).splitlines()[0])
                last_exc = e
    raise RuntimeError(f"All OpenStreetMap (Overpass) servers failed: {last_exc}")


def parse_osm(elements: list[dict], tag_type: dict[tuple[str, str], str]) -> list[dict]:
    out = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name") or tags.get("name:en")
        plat = el.get("lat", el.get("center", {}).get("lat"))
        plng = el.get("lon", el.get("center", {}).get("lon"))
        if not name or plat is None:
            continue
        email = tags.get("email") or tags.get("contact:email")
        out.append({
            "place_id": f"osm:{el['type']}/{el['id']}",
            "name": name,
            "category": next((t for (k, v), t in tag_type.items() if tags.get(k) == v), None),
            "address": _osm_address(tags),
            "lat": plat,
            "lng": plng,
            "phone": tags.get("phone") or tags.get("contact:phone") or tags.get("contact:mobile"),
            "website": tags.get("website") or tags.get("contact:website") or tags.get("url"),
            "emails": [email] if email else [],
            "source": "openstreetmap",
        })
    return out


def _osm_address(tags: dict) -> Optional[str]:
    if tags.get("addr:full"):
        return tags["addr:full"]
    keys = ("addr:housenumber", "addr:street", "addr:suburb", "addr:city", "addr:postcode")
    parts = [tags[k] for k in keys if tags.get(k)]
    return ", ".join(parts) or None


# ---------------- Google Places (paid, needs GOOGLE_PLACES_API_KEY) ----------------

def _google(settings: Settings, lat: float, lng: float, radius_m: int, types: list[str]) -> list[dict]:
    found: dict[str, dict] = {}
    for t in types:
        for p in _nearby(settings, GOOGLE_TYPES.get(t, [t]), lat, lng, radius_m, depth=0):
            if p["id"] in found:
                continue
            loc = p.get("location", {})
            found[p["id"]] = {
                "place_id": f"google:{p['id']}",
                "name": p.get("displayName", {}).get("text") or "?",
                "category": t,
                "address": p.get("formattedAddress"),
                "lat": loc.get("latitude"),
                "lng": loc.get("longitude"),
                "phone": p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber"),
                "website": p.get("websiteUri"),
                "emails": [],
                "source": "google",
            }
    return list(found.values())


def _nearby(settings: Settings, gtypes: list[str], lat: float, lng: float, radius_m: float, depth: int) -> list[dict]:
    body = {
        "includedTypes": gtypes,
        "maxResultCount": PLACES_MAX,
        "rankPreference": "DISTANCE",
        "locationRestriction": {"circle": {"center": {"latitude": lat, "longitude": lng}, "radius": radius_m}},
    }
    headers = {"X-Goog-Api-Key": settings.google_places_api_key, "X-Goog-FieldMask": PLACES_FIELDS}
    r = httpx.post(PLACES_URL, json=body, headers=headers, timeout=30)
    if r.status_code != 200:
        log.warning("Google Places %s for %s: %s", r.status_code, gtypes, r.text[:300])
        return []
    places = r.json().get("places", [])
    if len(places) < PLACES_MAX or depth >= PLACES_SPLIT_DEPTH:
        return places
    # A full page means there are probably more. Cover the circle with 4 overlapping
    # quarter circles (each centred on a quadrant of the bounding square) and search those.
    half = radius_m / 2
    dlat = half / 111_320
    dlng = half / (111_320 * math.cos(math.radians(lat)))
    for sy in (-1, 1):
        for sx in (-1, 1):
            places += _nearby(settings, gtypes, lat + sy * dlat, lng + sx * dlng, radius_m * 0.75, depth + 1)
    return places
