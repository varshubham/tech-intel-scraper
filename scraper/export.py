"""Excel / CSV export of upcoming events."""
from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from .core.db import DB

EVENT_COLUMNS = [
    ("id", "ID", 6), ("title", "Title", 50), ("start_date", "Start", 12), ("end_date", "End", 12),
    ("city", "City", 16), ("venue", "Venue", 30), ("mode", "Mode", 10), ("organizer", "Organizer", 28),
    ("price", "Price", 12), ("registration_url", "Link", 45), ("brochure_count", "Brochures", 10),
    ("source_names", "Found on", 22), ("description", "Description", 70), ("first_seen", "First seen", 20),
]
BROCHURE_COLUMNS = [
    ("event_id", "Event ID", 9), ("event_title", "Event", 45), ("url", "Brochure URL", 50),
    ("path", "Saved file", 40), ("pages", "Pages", 7), ("emails", "Emails", 35),
    ("phones", "Phones", 25), ("fees", "Fees", 25), ("text_excerpt", "Excerpt", 80),
]


BUSINESS_COLUMNS = [
    ("id", "ID", 6), ("name", "Business", 40), ("category", "Type", 12), ("distance_km", "Km", 7),
    ("phone", "Listed phone", 18), ("website", "Website", 35), ("emails", "Emails", 40),
    ("phones", "Phones on website", 30), ("people_text", "People", 60), ("address", "Address", 45),
    ("contact_pages", "Pages read", 45), ("source", "Found via", 14), ("first_seen", "First seen", 20),
]
PEOPLE_COLUMNS = [
    ("business_id", "Business ID", 11), ("business", "Business", 40), ("name", "Name", 28),
    ("designation", "Designation", 40), ("email", "Email", 30), ("phone", "Phone", 18), ("page", "Found on", 50),
]


def _flatten(v):
    if isinstance(v, list):
        return ", ".join(str(x.get("text", x)) if isinstance(x, dict) else str(x) for x in v)
    return v


def _rows(db: DB, upcoming: bool):
    events = db.list_events(upcoming=upcoming, limit=100000)
    brochures = []
    for e in events:
        e["brochure_count"] = len(e["brochures"])
        for b in e["brochures"]:
            brochures.append({**b, "event_title": e["title"]})
    return events, brochures


def _sheet(ws, columns, rows):
    ws.append([label for _, label, _ in columns])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    for r in rows:
        ws.append([_flatten(r.get(key)) for key, _, _ in columns])
    for i, (_, _, width) in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions


def to_xlsx_bytes(db: DB, upcoming: bool = True) -> bytes:
    events, brochures = _rows(db, upcoming)
    wb = Workbook()
    ws = wb.active
    ws.title = "Events"
    _sheet(ws, EVENT_COLUMNS, events)
    _sheet(wb.create_sheet("Brochures"), BROCHURE_COLUMNS, brochures)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def to_csv_text(db: DB, upcoming: bool = True) -> str:
    events, _ = _rows(db, upcoming)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([label for _, label, _ in EVENT_COLUMNS])
    for e in events:
        w.writerow([_flatten(e.get(key)) for key, _, _ in EVENT_COLUMNS])
    return buf.getvalue()


def write_leads_export(db: DB, out_dir: Path, category: str = "") -> Path:
    businesses = db.list_businesses(category)
    people = []
    for b in businesses:
        b["people_text"] = "; ".join(f"{p['name']} ({p.get('designation') or '?'})" for p in b["people"])
        people += [{**p, "business_id": b["id"], "business": b["name"]} for p in b["people"]]
    wb = Workbook()
    ws = wb.active
    ws.title = "Businesses"
    _sheet(ws, BUSINESS_COLUMNS, businesses)
    _sheet(wb.create_sheet("People"), PEOPLE_COLUMNS, people)
    path = out_dir / f"leads-{datetime.now().strftime('%Y%m%d-%H%M')}.xlsx"
    wb.save(path)
    return path


def write_export(db: DB, out_dir: Path, fmt: str = "xlsx", upcoming: bool = True) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    path = out_dir / f"events-{stamp}.{fmt}"
    if fmt == "xlsx":
        path.write_bytes(to_xlsx_bytes(db, upcoming))
    else:
        path.write_text(to_csv_text(db, upcoming), encoding="utf-8")
    return path
