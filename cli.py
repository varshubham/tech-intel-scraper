"""Command line entry point.

  python cli.py crawl [--source NAME]     scrape all (or one) sources into the DB
  python cli.py test-source NAME          dry run: show what a source yields, save nothing
  python cli.py export [--format csv]     write data/exports/events-<time>.xlsx
  python cli.py leads [--lat --lng ...]   businesses near a point + their contacts -> leads-<time>.xlsx
  python cli.py site [--out site]         events page as static files (index.html + xlsx + csv)
  python cli.py sources                   list configured sources
  python cli.py serve                     web UI + API + scheduler
"""
from __future__ import annotations

import argparse
import json
import logging
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Tech events scraper")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("crawl", help="scrape sources into the database")
    p.add_argument("--source", help="only this source name")

    p = sub.add_parser("test-source", help="dry run one source, print events")
    p.add_argument("name")
    p.add_argument("--no-cache", action="store_true")

    p = sub.add_parser("export", help="export upcoming events")
    p.add_argument("--format", choices=["xlsx", "csv"], default="xlsx")
    p.add_argument("--all", action="store_true", help="include past events")

    p = sub.add_parser("leads", help="find businesses near a point and collect their contacts")
    p.add_argument("--lat", type=float, help="default LEAD_LAT")
    p.add_argument("--lng", type=float, help="default LEAD_LNG")
    p.add_argument("--radius-km", type=float, help="default LEAD_RADIUS_KM (6)")
    p.add_argument("--types", help="comma-separated, e.g. hospital,clinic (default LEAD_TYPES)")
    p.add_argument("--no-contacts", action="store_true", help="only list businesses, skip their websites")
    p.add_argument("--dry-run", action="store_true", help="print results, save nothing")

    p = sub.add_parser("site", help="write the events page as static files (GitHub Pages)")
    p.add_argument("--out", default="site", help="output folder (default: site)")

    sub.add_parser("sources", help="list enabled sources")
    sub.add_parser("serve", help="run web UI / API / scheduler")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from scraper.core.config import load_settings, load_sources

    settings = load_settings()

    if args.cmd == "crawl":
        from scraper.events.pipeline import run_crawl

        summary = run_crawl(settings, args.source)
        print(json.dumps(summary, indent=2))
        return 0 if all(s.get("status") == "ok" for s in summary) else 1

    if args.cmd == "test-source":
        from scraper.core.fetcher import Fetcher
        from scraper.events import filters
        from scraper.events.pipeline import extract_events

        src = next((s for s in load_sources(settings) if s["name"] == args.name), None)
        if not src:
            print(f"No enabled source named {args.name!r}", file=sys.stderr)
            return 2
        if args.no_cache:
            settings.cache_ttl_hours = 0
        fetcher = Fetcher(settings)
        try:
            raw = extract_events(fetcher, settings, src)
        finally:
            fetcher.close()
        for ev in raw:
            ev = filters.clean(ev)
            tech = src.get("tech_only") or filters.is_tech(ev, settings.keywords)
            up = filters.is_upcoming(ev, keep_undated=True)
            local = filters.in_country(ev, src.get("country", ""))
            skip = filters.title_excluded(ev, src.get("exclude_titles", ""))
            flag = ("KEEP" if tech and up and local and not skip else
                    "not-tech" if not tech else "abroad" if not local else "excluded" if skip else "past")
            print(f"[{flag:8}] {ev.get('start_date') or '?':10}  {ev['title'][:80]}  | {ev.get('city') or ev.get('mode') or ''}")
        print(f"\n{len(raw)} raw events")
        return 0

    if args.cmd == "export":
        from scraper.core.db import DB
        from scraper.export import write_export

        path = write_export(DB(settings.db_path), settings.exports_dir, args.format, upcoming=not args.all)
        print(path)
        return 0

    if args.cmd == "leads":
        from scraper.core.db import DB
        from scraper.export import write_leads_export
        from scraper.leads.pipeline import run_leads

        lat = args.lat if args.lat is not None else settings.lead_lat
        lng = args.lng if args.lng is not None else settings.lead_lng
        if lat is None or lng is None:
            print("Set LEAD_LAT and LEAD_LNG in .env, or pass --lat and --lng", file=sys.stderr)
            return 2
        radius = args.radius_km or settings.lead_radius_km
        types = [t.strip().lower() for t in args.types.split(",")] if args.types else settings.lead_types
        db = None if args.dry_run else DB(settings.db_path)
        found = run_leads(settings, lat, lng, radius, types, contacts=not args.no_contacts, db=db)
        for b in found:
            contacts = ", ".join(b["emails"][:2] + b["phones"][:2]) or b.get("phone") or "-"
            people = "; ".join(f"{p['name']} ({(p.get('designation') or '')[:30]})" for p in b["people"][:3])
            print(f"{b['distance_km']:5.1f} km  {b['category'] or '?':10} {b['name'][:45]:45}  {contacts}")
            if people:
                print(f"{'':19}people: {people}")
        print(f"\n{len(found)} businesses")
        if db:
            print(write_leads_export(db, settings.exports_dir))
        return 0

    if args.cmd == "site":
        from pathlib import Path

        from scraper.core.db import DB
        from scraper.site import write_site

        print(write_site(DB(settings.db_path), Path(args.out)))
        return 0

    if args.cmd == "sources":
        for s in load_sources(settings):
            print(f"{s['name']:28} {s.get('type', 'static'):7} {s.get('url') or s['urls'][0]}")
        return 0

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("scraper.api:app", host=settings.host, port=settings.port, workers=1)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
