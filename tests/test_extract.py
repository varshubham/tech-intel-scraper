from datetime import date
from pathlib import Path

import pytest

from scraper.core.db import DB
from scraper.events import extract, filters
from scraper.events.brochures import analyse_text
from scraper.events.dates import parse_date, parse_date_range

FIXTURES = Path(__file__).parent / "fixtures"
KEYWORDS = {"tech": ["AI", "IT", "cloud", "technology"], "exclude": ["yoga"]}


def test_jsonld_events():
    soup = extract.soupify((FIXTURES / "jsonld_listing.html").read_text())
    events = extract.extract_jsonld(soup, "https://example.com/list")
    assert len(events) == 2
    summit, meetup = events
    assert summit["title"] == "Bengaluru AI Summit 2099"
    assert summit["start_date"] == "2099-11-21"
    assert summit["end_date"] == "2099-11-22"
    assert summit["city"] == "Bengaluru"
    assert summit["venue"] == "KTPO"
    assert summit["mode"] == "offline"
    assert summit["organizer"] == "AI Org"
    assert summit["price"] == "Free"
    assert summit["registration_url"] == "https://example.com/e/ai-summit"
    assert meetup["mode"] == "online"  # trailing comma in JSON was tolerated


def test_brochure_links():
    soup = extract.soupify((FIXTURES / "jsonld_listing.html").read_text())
    links = extract.find_brochure_links(soup, "https://example.com/list")
    assert links == ["https://example.com/files/brochure.pdf", "https://example.com/agenda"]


def test_selectors():
    html = """<div class="card"><h3><a href="/e/1">DevOps Day</a></h3><span class="d">4 - 9 Feb 2099</span>
              <span class="c">Pune</span></div>"""
    cfg = {"item": ".card", "fields": {"title": "h3 a", "link": "h3 a@href", "date": ".d", "city": ".c"}}
    [ev] = extract.extract_with_selectors(extract.soupify(html), "https://x.org/", cfg)
    assert ev["title"] == "DevOps Day"
    assert ev["registration_url"] == "https://x.org/e/1"
    assert (ev["start_date"], ev["end_date"]) == ("2099-02-04", "2099-02-09")
    assert ev["city"] == "Pune"


@pytest.mark.parametrize("text,expected", [
    ("21st-23rd Nov, 2099", ("2099-11-21", "2099-11-23")),
    ("10 Aug - 12 Nov, 2099", ("2099-08-10", "2099-11-12")),
    ("Nov 21-23, 2099", ("2099-11-21", "2099-11-23")),
    ("2099-11-21T09:00:00Z", ("2099-11-21", None)),
    ("05 December 2099", ("2099-12-05", None)),
    ("July 21, 2099 – July 31, 2099 @ All Day – India hosted", ("2099-07-21", "2099-07-31")),
    ("October 18, 2099 @ 1:00 pm – 2:00 pm – Talk show", ("2099-10-18", None)),
    ("Offline Open Starts 20/11/99", ("2099-11-20", None)),
    ("20–21 Nov 2099, Bangalore & Hybrid", ("2099-11-20", "2099-11-21")),
])
def test_date_ranges(text, expected):
    assert parse_date_range(text) == expected


def test_parse_date_dmy():
    assert parse_date("05-06-2099") == "2099-06-05"  # Indian day-first order


def test_parse_date_two_digit_year_today():
    today = date.today()
    assert parse_date(today.strftime("%d %b %y")) == today.isoformat()  # not 21xx


def test_tech_filter_acronyms_are_case_sensitive():
    assert filters.is_tech({"title": "IT Leaders Forum"}, KEYWORDS)
    assert not filters.is_tech({"title": "Make it happen", "description": "join it"}, KEYWORDS)
    assert filters.is_tech({"title": "Cloud native day"}, KEYWORDS)
    assert not filters.is_tech({"title": "AI yoga retreat"}, KEYWORDS)


def test_clean_and_upcoming():
    ev = filters.clean({"title": "  X  ", "city": "N/A", "start_date": "2000-01-01", "end_date": "1999-01-01"})
    assert ev["title"] == "X" and ev["city"] is None and ev["end_date"] is None
    assert not filters.is_upcoming(ev)
    assert filters.is_upcoming({"start_date": "2000-01-01", "end_date": "2099-01-01"})


def test_brochure_text_analysis():
    info = analyse_text("Contact: events@expo.in, +91 98765 43210. Fee ₹2,500 + GST. Dates 21-23 Nov 2099. 4 hours")
    assert info["emails"] == ["events@expo.in"]
    assert any("98765" in p for p in info["phones"])
    assert info["fees"] == ["₹2,500 + GST"]
    assert info["dates"][0]["start"] == "2099-11-21"


def test_db_dedupe(tmp_path):
    db = DB(tmp_path / "t.db")
    a, new_a = db.upsert_event({"title": "Bengaluru AI Summit 2099", "start_date": "2099-11-21",
                                "source_url": "https://a"}, "site-a")
    b, new_b = db.upsert_event({"title": "AI Summit 2099 Bengaluru", "start_date": "2099-11-21",
                                "city": "Bengaluru", "source_url": "https://b"}, "site-b")
    assert new_a and not new_b and a == b
    [ev] = db.list_events()
    assert ev["city"] == "Bengaluru"
    assert ev["source_names"] == ["site-a", "site-b"]
