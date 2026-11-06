"""Turn messy date strings ('21st-23rd Nov, 2026', '2026-11-21T09:00+05:30') into ISO dates."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

import dateparser

_DP_SETTINGS = {"DATE_ORDER": "DMY", "PREFER_DATES_FROM": "future", "RETURN_AS_TIMEZONE_AWARE": False}
_ORD = r"(?:st|nd|rd|th)?"
_SHORT_YEAR = re.compile(r"^(\d{1,2}\s+[A-Za-z]{3,9}\.?,?)\s+(\d{2})$")  # 22 Oct 26
_SEP = r"\s*(?:-|–|—|to|till|until)\s*"

# 21-23 Nov 2026 | 21st – 23rd November, 2026
_RANGE_SAME_MONTH = re.compile(rf"\b(\d{{1,2}}){_ORD}{_SEP}(\d{{1,2}}){_ORD}\s+([A-Za-z]{{3,9}})\.?,?\s+(\d{{4}})\b")
# 30 Nov - 2 Dec 2026
_RANGE_TWO_MONTHS = re.compile(
    rf"\b(\d{{1,2}}){_ORD}\s+([A-Za-z]{{3,9}})\.?{_SEP}(\d{{1,2}}){_ORD}\s+([A-Za-z]{{3,9}})\.?,?\s+(\d{{4}})\b"
)
# Nov 21-23, 2026
_RANGE_MONTH_FIRST = re.compile(rf"\b([A-Za-z]{{3,9}})\.?\s+(\d{{1,2}}){_ORD}{_SEP}(\d{{1,2}}){_ORD},?\s+(\d{{4}})\b")
# Starts 20/11/26 (day first, inside other text)
_NUMERIC_DMY = re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})\b")
# October 7, 2026 – October 10, 2026 @ 12:00 am  (end date optional, trailing text ignored)
_FULL_MONTH_FIRST = re.compile(
    rf"\b([A-Za-z]{{3,9}})\.?\s+(\d{{1,2}}){_ORD},?\s+(\d{{4}})\b"
    rf"(?:{_SEP}([A-Za-z]{{3,9}})\.?\s+(\d{{1,2}}){_ORD},?\s+(\d{{4}})\b)?"
)


def parse_date(value) -> Optional[str]:
    """Single date -> 'YYYY-MM-DD' or None."""
    if not value:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    try:  # ISO 8601 (what JSON-LD gives us) is the common fast path
        return datetime.fromisoformat(s.replace("Z", "+00:00")).strftime("%Y-%m-%d")
    except ValueError:
        pass
    # '22 Oct 26': dateparser's prefer-future pushes today's date to 2126, so expand the year first
    m = _SHORT_YEAR.match(s)
    if m:
        s = f"{m.group(1)} 20{m.group(2)}"
    dt = dateparser.parse(s, settings=_DP_SETTINGS)
    return dt.strftime("%Y-%m-%d") if dt else None


def parse_date_range(text: str) -> tuple[Optional[str], Optional[str]]:
    """Free text containing a date or date range -> (start, end)."""
    if not text:
        return None, None
    t = " ".join(str(text).split())

    m = _RANGE_TWO_MONTHS.search(t)
    if m:
        d1, m1, d2, m2, y = m.groups()
        return parse_date(f"{d1} {m1} {y}"), parse_date(f"{d2} {m2} {y}")
    m = _RANGE_SAME_MONTH.search(t)
    if m:
        d1, d2, mon, y = m.groups()
        return parse_date(f"{d1} {mon} {y}"), parse_date(f"{d2} {mon} {y}")
    m = _RANGE_MONTH_FIRST.search(t)
    if m:
        mon, d1, d2, y = m.groups()
        return parse_date(f"{d1} {mon} {y}"), parse_date(f"{d2} {mon} {y}")
    m = _FULL_MONTH_FIRST.search(t)
    if m:
        m1, d1, y1, m2, d2, y2 = m.groups()
        start = parse_date(f"{d1} {m1} {y1}")
        if start:
            return start, parse_date(f"{d2} {m2} {y2}") if m2 else None

    for sep in (" - ", " – ", " to "):
        if sep in t:
            a, _, b = t.partition(sep)
            start, end = parse_date(a), parse_date(b)
            if start and end:
                return start, end
    single = parse_date(t)
    if not single:
        m = _NUMERIC_DMY.search(t)
        if m:
            single = parse_date("/".join(m.groups()))
    return single, None
