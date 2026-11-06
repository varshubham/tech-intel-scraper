# Tech Events Scraper

Collects **upcoming tech events** (and their **brochure PDFs**) from government, industry and event websites into one list. View it in a browser, download it as Excel or CSV, or read it as JSON.

- No paid APIs and no LLM needed. It reads schema.org data and CSS selectors. A local Ollama model is an optional fallback.
- Polite by default: respects robots.txt, waits 3 s between requests to the same site, caches pages, retries with backoff.
- Ships as one Docker container with SQLite. Nothing else to run.

## Quick start (local)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env

python cli.py crawl            # scrape all sources (~2–3 min)
python cli.py export           # -> data/exports/events-<time>.xlsx
python cli.py serve            # http://localhost:8000
```

Other commands:

```bash
python cli.py sources                    # list enabled sources
python cli.py test-source nasscom        # dry run: shows what a source yields, saves nothing
python cli.py crawl --source stpi        # crawl one source
python cli.py export --format csv --all  # include past events
```

## Deploy

### Any VPS (DigitalOcean, AWS EC2, Hetzner…), recommended

```bash
cp .env.example .env        # set API_TOKEN
docker compose up -d --build
```

- Web UI at `http://<server>:8000`. The built-in scheduler crawls every `CRAWL_INTERVAL_HOURS` (default 24).
- `./data` holds the database, brochures, exports and cache, and survives restarts and redeploys.
- `./config` is mounted read-only, so after editing `sources.yaml` just run `docker compose restart`.

### Render / Railway / Fly.io

Point the platform at this repo; it builds the `Dockerfile` automatically.
- Attach a **persistent disk/volume at `/app/data`**. Without one, data is lost on every redeploy.
- Set environment variables from `.env.example` (at least `API_TOKEN`). `PORT` is picked up automatically.
- Run a **single instance**. The scheduler and SQLite assume one process.

### Cron instead of the built-in scheduler

Set `CRAWL_INTERVAL_HOURS=0` and run the crawl from cron (or a platform cron job):

```cron
0 6 * * *  cd /opt/tech-events && docker compose run --rm scraper python cli.py crawl
```

### Trigger a crawl over HTTP

```bash
curl -X POST -H "Authorization: Bearer $API_TOKEN" https://<host>/api/crawl
curl -X POST -H "Authorization: Bearer $API_TOKEN" "https://<host>/api/crawl?source=nasscom"
```

## Endpoints

| Path | What |
|---|---|
| `/` | HTML table of upcoming events with search and city filter |
| `/api/events?q=&city=&upcoming=true&limit=&offset=` | JSON |
| `/export/events.xlsx`, `/export/events.csv` | downloads (Excel has an Events sheet and a Brochures sheet) |
| `/brochures/{id}` | the saved PDF |
| `/api/runs` | crawl log per source (found / new / errors) |
| `POST /api/crawl` | start a crawl (needs `API_TOKEN`) |
| `/health` | health check |

## Adding a website

Edit `config/sources.yaml`. No code changes are needed.

1. **Check whether the site has schema.org data.** Add just `name` and `url`, then run `python cli.py test-source <name>`. If events print, you're done; Eventbrite, Meetup, AllEvents and many conference sites work this way.
2. **Otherwise add CSS selectors.** In the browser, right-click an event card and choose Inspect. Then write:
   ```yaml
   - name: my-site
     url: https://example.gov.in/events
     tech_only: true
     selectors:
       item: .event-card              # one event card
       fields:
         title: h3 a
         link: h3 a@href              # @attr reads an attribute
         date: .date                  # free text like "21-23 Nov 2026" is fine
         city: .location
         brochure: a[href$=".pdf"]@href
   ```
3. **If the page is built by JavaScript** (empty HTML), set `type: js`, build with `INSTALL_PLAYWRIGHT=true` and set `USE_PLAYWRIGHT=true`.

Which events count as "tech" is controlled by `config/keywords.yaml`.

## How it works

```
sources.yaml ─► fetch listing (robots.txt check, delay, cache, retries)
             ─► extract: schema.org JSON-LD → CSS selectors → (optional) Ollama
             ─► clean + tech keyword filter
             ─► open event page (fill gaps, find brochure links)      [follow_links]
             ─► keep upcoming only
             ─► SQLite upsert, merging the same event from different sites
                (fuzzy title match + same start date)
             ─► download PDFs, dedupe by SHA-256, extract emails / phones / fees / dates
```

Code lives in `scraper/core` (config, fetcher, db), `scraper/events` (extract, dates, filters, brochures, pipeline) and `scraper/leads` (places, contacts, pipeline). The API is `scraper/api.py`.

## Leads: businesses near a location

Finds businesses of the types you choose around a point, reads their websites, and collects
emails, phone numbers and named people (directors, administrators, purchase managers…).

```bash
# in .env: LEAD_LAT, LEAD_LNG, LEAD_RADIUS_KM=6, LEAD_TYPES=hospital,clinic
python cli.py leads                       # saves to the DB, writes data/exports/leads-<time>.xlsx
python cli.py leads --lat 28.61 --lng 77.21 --types hospital --radius-km 5 --dry-run
python cli.py leads --no-contacts         # just the list of businesses, skip the websites
```

```
OpenStreetMap (free) or Google Places (GOOGLE_PLACES_API_KEY) ─► businesses within the radius
  ─► home page + up to 3 contact / team / about pages (robots.txt, delay, cache)
     JavaScript-only sites are re-read through Jina Reader
  ─► emails, phones, "name + job title" pairs (or Ollama when USE_LLM=true)
  ─► SQLite `businesses` table ─► xlsx: Businesses sheet + People sheet
```

OpenStreetMap is free but many Indian businesses there have no website or phone. Google Places
finds more and almost always has a phone and website (paid per request; a 6 km search is a few calls).
People found on websites are a starting point for sales, not verified contacts. Named individuals'
details are personal data under India's DPDP Act 2023, so check your lawful basis before outreach.

## Current sources

| Source | Method | Notes |
|---|---|---|
| NASSCOM | CSS selectors | follows event pages for brochures |
| STPI | CSS selectors | mostly lists past events; picks up new ones when posted |
| Eventbrite India (6 cities) | JSON-LD | |
| Meetup India (5 cities) | JSON-LD | |
| AllEvents (5 cities) | JSON-LD | |
| MeitY (Digital India portal) | CSS selectors | last 3 archive pages + upcoming widget |
| Hasgeek | CSS selectors + event-page JSON-LD | Rootconf, Fifth Elephant, community meetups |
| Devfolio | CSS selectors | hackathons; no Playwright needed |
| dev.events (India) | JSON-LD | developer conferences across Indian cities |
| AIM Research | JSON-LD | AI / data / CXO events; `country: IN` drops the ones abroad |
| Luma (Bengaluru, Delhi, Mumbai) | JSON-LD | mixed calendars, keyword filter keeps the tech ones |
| KonfHub | needs Playwright | disabled by default |
| 10times | returns 403 to bots | disabled |

## Limitations

- Scanned (image-only) brochure PDFs are saved, but their text isn't extracted because OCR isn't wired in yet.
- Websites change their HTML. If a source's `found` count drops to 0 in `/api/runs`, update its selectors.
- Tests: `pip install pytest && pytest`
