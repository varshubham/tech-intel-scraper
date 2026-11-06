"""SQLite storage. One file, zero setup; mount data/ as a volume when deployed."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Iterator, Optional

from rapidfuzz import fuzz

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    start_date TEXT,
    end_date TEXT,
    city TEXT,
    venue TEXT,
    mode TEXT,
    organizer TEXT,
    category TEXT,
    description TEXT,
    price TEXT,
    registration_url TEXT,
    sources TEXT NOT NULL DEFAULT '[]',
    source_names TEXT NOT NULL DEFAULT '[]',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_date);

CREATE TABLE IF NOT EXISTS brochures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    pages INTEGER,
    emails TEXT,
    phones TEXT,
    fees TEXT,
    dates TEXT,
    text_excerpt TEXT,
    downloaded_at TEXT NOT NULL,
    UNIQUE(event_id, sha256)
);

CREATE TABLE IF NOT EXISTS businesses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    category TEXT,
    address TEXT,
    lat REAL,
    lng REAL,
    distance_km REAL,
    phone TEXT,
    website TEXT,
    emails TEXT NOT NULL DEFAULT '[]',
    phones TEXT NOT NULL DEFAULT '[]',
    people TEXT NOT NULL DEFAULT '[]',
    contact_pages TEXT NOT NULL DEFAULT '[]',
    source TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT,
    found INTEGER DEFAULT 0,
    new INTEGER DEFAULT 0,
    error TEXT
);
"""

EVENT_FIELDS = (
    "title", "start_date", "end_date", "city", "venue", "mode", "organizer",
    "category", "description", "price", "registration_url",
)
DEDUPE_THRESHOLD = 88


def _now() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: Path):
        self.path = path
        with self.conn() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA journal_mode = WAL")  # lets the API read while a crawl writes
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # ---------- events ----------

    def upsert_event(self, ev: dict, source_name: str) -> tuple[int, bool]:
        """Insert, or merge into an existing event with a near-identical title and same start date.
        Returns (event_id, is_new)."""
        now = _now()
        with self.conn() as c:
            existing = self._find_duplicate(c, ev)
            if existing:
                merged = {f: existing[f] or ev.get(f) for f in EVENT_FIELDS}
                # prefer the longer description
                if ev.get("description") and len(ev["description"]) > len(existing["description"] or ""):
                    merged["description"] = ev["description"]
                sources = _merge_list(existing["sources"], ev.get("source_url"))
                names = _merge_list(existing["source_names"], source_name)
                sets = ", ".join(f"{f} = ?" for f in EVENT_FIELDS)
                c.execute(
                    f"UPDATE events SET {sets}, sources = ?, source_names = ?, last_seen = ? WHERE id = ?",
                    [merged[f] for f in EVENT_FIELDS] + [sources, names, now, existing["id"]],
                )
                return existing["id"], False

            cols = ", ".join(EVENT_FIELDS)
            qs = ", ".join("?" for _ in EVENT_FIELDS)
            cur = c.execute(
                f"INSERT INTO events ({cols}, sources, source_names, first_seen, last_seen) "
                f"VALUES ({qs}, ?, ?, ?, ?)",
                [ev.get(f) for f in EVENT_FIELDS]
                + [json.dumps([ev["source_url"]] if ev.get("source_url") else []),
                   json.dumps([source_name]), now, now],
            )
            return cur.lastrowid, True

    def _find_duplicate(self, c: sqlite3.Connection, ev: dict) -> Optional[sqlite3.Row]:
        if ev.get("start_date"):
            rows = c.execute("SELECT * FROM events WHERE start_date = ?", (ev["start_date"],)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM events WHERE start_date IS NULL AND registration_url = ?",
                (ev.get("registration_url"),),
            ).fetchall()
        title = ev["title"].lower()
        for row in rows:
            if fuzz.token_set_ratio(title, row["title"].lower()) >= DEDUPE_THRESHOLD:
                return row
        return None

    def list_events(self, upcoming: bool = True, q: str = "", city: str = "",
                    limit: int = 500, offset: int = 0) -> list[dict]:
        sql = "SELECT * FROM events WHERE 1=1"
        args: list = []
        if upcoming:
            sql += " AND COALESCE(end_date, start_date) >= ?"
            args.append(date.today().isoformat())
        if q:
            sql += " AND (title LIKE ? OR description LIKE ? OR organizer LIKE ?)"
            args += [f"%{q}%"] * 3
        if city:
            sql += " AND city LIKE ?"
            args.append(f"%{city}%")
        sql += " ORDER BY start_date IS NULL, start_date LIMIT ? OFFSET ?"
        args += [limit, offset]
        with self.conn() as c:
            events = [_row_to_dict(r) for r in c.execute(sql, args).fetchall()]
            if events:
                ids = [e["id"] for e in events]
                marks = ",".join("?" * len(ids))
                bro = c.execute(f"SELECT * FROM brochures WHERE event_id IN ({marks})", ids).fetchall()
                by_event: dict[int, list] = {}
                for b in bro:
                    by_event.setdefault(b["event_id"], []).append(_row_to_dict(b))
                for e in events:
                    e["brochures"] = by_event.get(e["id"], [])
        return events

    # ---------- brochures ----------

    def has_brochure(self, event_id: int, url: str) -> bool:
        with self.conn() as c:
            return c.execute(
                "SELECT 1 FROM brochures WHERE event_id = ? AND url = ?", (event_id, url)
            ).fetchone() is not None

    def add_brochure(self, event_id: int, info: dict) -> None:
        with self.conn() as c:
            c.execute(
                "INSERT OR IGNORE INTO brochures (event_id, url, sha256, path, pages, emails, phones, "
                "fees, dates, text_excerpt, downloaded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, info["url"], info["sha256"], info["path"], info.get("pages"),
                 json.dumps(info.get("emails", [])), json.dumps(info.get("phones", [])),
                 json.dumps(info.get("fees", [])), json.dumps(info.get("dates", [])),
                 info.get("text_excerpt"), _now()),
            )

    def get_brochure(self, brochure_id: int) -> Optional[dict]:
        with self.conn() as c:
            r = c.execute("SELECT * FROM brochures WHERE id = ?", (brochure_id,)).fetchone()
            return _row_to_dict(r) if r else None

    # ---------- businesses (leads) ----------

    def upsert_business(self, b: dict) -> None:
        now = _now()
        cols = ("place_id", "name", "category", "address", "lat", "lng", "distance_km", "phone", "website")
        lists = ("emails", "phones", "people", "contact_pages")
        values = [b.get(c) for c in cols] + [json.dumps(b.get(k) or []) for k in lists]
        updates = ", ".join(f"{c} = excluded.{c}" for c in cols[1:] + lists)
        with self.conn() as c:
            c.execute(
                f"INSERT INTO businesses ({', '.join(cols + lists)}, source, first_seen, last_seen) "
                f"VALUES ({', '.join('?' * (len(cols) + len(lists)))}, ?, ?, ?) "
                f"ON CONFLICT(place_id) DO UPDATE SET {updates}, last_seen = excluded.last_seen",
                values + [b.get("source"), now, now],
            )

    def list_businesses(self, category: str = "", limit: int = 100000) -> list[dict]:
        sql = "SELECT * FROM businesses"
        args: list = []
        if category:
            sql += " WHERE category = ?"
            args.append(category)
        sql += " ORDER BY distance_km LIMIT ?"
        with self.conn() as c:
            return [_row_to_dict(r) for r in c.execute(sql, args + [limit]).fetchall()]

    # ---------- runs ----------

    def start_run(self, source: str) -> int:
        with self.conn() as c:
            return c.execute(
                "INSERT INTO crawl_runs (source, started_at, status) VALUES (?, ?, 'running')",
                (source, _now()),
            ).lastrowid

    def finish_run(self, run_id: int, status: str, found: int = 0, new: int = 0,
                   error: Optional[str] = None) -> None:
        with self.conn() as c:
            c.execute(
                "UPDATE crawl_runs SET finished_at = ?, status = ?, found = ?, new = ?, error = ? WHERE id = ?",
                (_now(), status, found, new, error, run_id),
            )

    def list_runs(self, limit: int = 50) -> list[dict]:
        with self.conn() as c:
            rows = c.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [_row_to_dict(r) for r in rows]


def _merge_list(existing_json: str, item: Optional[str]) -> str:
    items = json.loads(existing_json or "[]")
    if item and item not in items:
        items.append(item)
    return json.dumps(items)


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("sources", "source_names", "emails", "phones", "fees", "dates", "people", "contact_pages"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d
