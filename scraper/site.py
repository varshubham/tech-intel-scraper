"""The events web page: served live by api.py, or written as static files for GitHub Pages (python cli.py site)."""
from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from . import export
from .core.db import DB

# Static pages have no server to filter on, so the search boxes filter the table in the browser
FILTER_JS = """<script>
const q = document.querySelector('input[name=q]'), c = document.querySelector('input[name=city]');
const rows = [...document.querySelectorAll('tbody tr')];
function filter() {
  const qv = q.value.toLowerCase(), cv = c.value.toLowerCase();
  rows.forEach(r => {
    const show = r.textContent.toLowerCase().includes(qv) && r.cells[2].textContent.toLowerCase().includes(cv);
    r.style.display = show ? '' : 'none';
  });
}
q.addEventListener('input', filter); c.addEventListener('input', filter);
</script>"""


def render_events_page(events: list[dict], q: str = "", city: str = "", static: bool = False) -> str:
    e = html.escape
    rows = []
    for ev in events:
        when = e(ev["start_date"] or "?") + (f" → {e(ev['end_date'])}" if ev["end_date"] else "")
        title = e(ev["title"])
        if ev["registration_url"]:
            title = f'<a href="{e(ev["registration_url"])}" target="_blank" rel="noopener">{title}</a>'
        # static pages don't carry the saved PDFs, so they link to the brochure's original address
        pdfs = " ".join(
            f'<a href="{e(b["url"]) if static else "/brochures/%d" % b["id"]}">PDF</a>' for b in ev["brochures"]
        )
        place = ", ".join(x for x in (ev["venue"], ev["city"]) if x) or (ev["mode"] or "")
        rows.append(
            f"<tr><td>{when}</td><td>{title}</td><td>{e(place)}</td>"
            f"<td>{e(ev['organizer'] or '')}</td><td>{pdfs}</td><td>{e(', '.join(ev['source_names']))}</td></tr>"
        )
    if static:
        updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        links = f'<a href="events.xlsx">Download Excel</a> · <a href="events.csv">CSV</a> · updated {updated}'
        button, script = "", FILTER_JS
    else:
        links = '<a href="/export/events.xlsx">Download Excel</a> · <a href="/export/events.csv">CSV</a>' \
                ' · <a href="/api/runs">Crawl log</a>'
        button, script = "<button>Filter</button> ", ""
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Upcoming Tech Events</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;padding:16px;background:#fafafa;color:#222}}
table{{border-collapse:collapse;width:100%;background:#fff;font-size:14px}}
th,td{{padding:8px;border-bottom:1px solid #eee;text-align:left;vertical-align:top}}
th{{background:#1f4e78;color:#fff;position:sticky;top:0}} a{{color:#1f4e78}}
form{{margin:12px 0;display:flex;gap:8px;flex-wrap:wrap}} input{{padding:6px}}
.wrap{{overflow-x:auto}}
</style></head><body>
<h1>Upcoming Tech Events <small style="font-weight:normal;font-size:14px">({len(events)})</small></h1>
<form><input name="q" placeholder="Search" value="{e(q)}"><input name="city" placeholder="City" value="{e(city)}">
{button}{links}</form>
<div class="wrap"><table><thead><tr><th>When</th><th>Event</th><th>Where</th><th>Organizer</th><th>Brochure</th>
<th>Source</th></tr></thead><tbody>
{''.join(rows) or '<tr><td colspan="6">No events yet — run a crawl.</td></tr>'}
</tbody></table></div>{script}</body></html>"""


def write_site(db: DB, out_dir: Path) -> Path:
    """index.html + events.xlsx + events.csv, ready to publish as a static site."""
    out_dir.mkdir(parents=True, exist_ok=True)
    events = db.list_events(upcoming=True, limit=100000)
    (out_dir / "index.html").write_text(render_events_page(events, static=True), encoding="utf-8")
    (out_dir / "events.xlsx").write_bytes(export.to_xlsx_bytes(db))
    (out_dir / "events.csv").write_text(export.to_csv_text(db), encoding="utf-8")
    return out_dir / "index.html"
