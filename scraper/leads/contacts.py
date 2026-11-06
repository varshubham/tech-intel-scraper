"""Visit a business website (home + contact/about/team pages) and pull out emails, phones and named people."""
from __future__ import annotations

import json
import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from ..core.config import Settings
from ..core.fetcher import BlockedByRobots, Fetcher
from ..events.brochures import EMAIL_RE, PHONE_RE
from ..events.extract import soupify

log = logging.getLogger(__name__)

# Pages worth opening besides the home page, best first
CONTACT_LINK_HINTS = [
    re.compile(p, re.I) for p in (
        r"contact|reach[-_ ]?us|get[-_ ]?in[-_ ]?touch|enquir|inquir",
        r"management|leadership|board|director|administration|our[-_ ]?team|team",
        r"about|who[-_ ]?we[-_ ]?are",
    )
]
# Sites where a "website" is just a profile page with nothing to extract
SKIP_DOMAINS = ("facebook.com", "instagram.com", "linkedin.com", "twitter.com", "x.com", "youtube.com",
                "wa.me", "whatsapp.com", "google.com", "justdial.com", "practo.com")
JUNK_EMAIL = re.compile(r"\.(png|jpe?g|gif|svg|webp)$|@(example|domain|email|sentry|wixpress|yourdomain)\.", re.I)

ROLE_RE = re.compile(
    r"\b(CEO|COO|CFO|CTO|CIO|Chairman|Chairperson|Founder|Co-Founder|Owner|Partner|President|"
    r"(?:Managing |Medical |Executive |Joint )?Director|Medical Superintendent|Administrator|Principal|Dean|"
    r"Head|Manager|Purchase|Procurement|Superintendent|Secretary|Trustee|Proprietor)\b",
    re.I,
)
NAME_RE = re.compile(
    r"(?:(?:Dr|Mr|Mrs|Ms|Shri|Smt|Prof|Col|Capt)\.?\s+)?[A-Z][a-z]+(?:\s+[A-Z]\.)*(?:\s+[A-Z][a-z]+){1,3}"
)
# Capitalised words that make a line a heading, not a person's name
NOT_NAME_WORDS = {
    "our", "team", "contact", "about", "us", "home", "hospital", "hospitals", "clinic", "services", "read",
    "more", "department", "centre", "center", "care", "medical", "management", "board", "directors",
    "leadership", "message", "view", "profile", "know", "the", "of", "and", "for", "with", "news", "health",
    "healthcare", "school", "college", "group", "private", "limited", "ltd", "india", "best", "book",
    "appointment", "call", "email", "address", "office", "trust", "foundation", "welcome", "meet",
    "padma", "bhushan", "vibhushan", "honorary", "hony", "organizational", "organisational", "structure",
    "web", "information", "chart", "message", "desk", "officer",
}
# Paths that match a hint word ("stress-management", "pain-management") but aren't contact pages
NOT_CONTACT_PATH = re.compile(r"/(blog|news|article|departments?|services?|events?|careers?|gallery|media)s?/", re.I)
MAX_LINE = 90
MIN_TEXT = 300  # less visible text than this = page probably needs JavaScript


def collect_contacts(fetcher: Fetcher, settings: Settings, website: str) -> dict:
    """Emails, phones and people found on a business website, plus which pages were read."""
    result: dict = {"emails": [], "phones": [], "people": [], "contact_pages": []}
    if not website or _is_skipped(urlparse(website).netloc.lower()):
        return result
    home = _page(fetcher, settings, website)
    if not home:
        return result
    pages = [home]
    for link in contact_links(home[1], home[0])[: max(settings.lead_max_pages - 1, 0)]:
        page = _page(fetcher, settings, link)
        if page:
            pages.append(page)

    for url, soup in pages:
        emails, phones = emails_and_phones(soup)
        result["emails"] = _merge(result["emails"], emails)
        result["phones"] = _merge(result["phones"], phones, key=lambda p: re.sub(r"\D", "", p)[-10:])
        for person in find_people(soup):
            person["page"] = url
            result["people"].append(person)
    if settings.use_llm:
        llm_people = people_with_llm(settings, pages)
        if llm_people:
            result["people"] = llm_people
    result["people"] = _merge([], result["people"], key=lambda p: p["name"].lower())
    result["contact_pages"] = [url for url, _ in pages[1:]]
    return result


def _is_skipped(netloc: str) -> bool:
    return any(netloc == d or netloc.endswith("." + d) for d in SKIP_DOMAINS)


def _page(fetcher: Fetcher, settings: Settings, url: str) -> Optional[tuple[str, BeautifulSoup]]:
    try:
        r = fetcher.get(url)
    except BlockedByRobots:
        log.info("robots.txt disallows %s", url)
        return None
    except Exception as e:
        log.info("Could not fetch %s: %s", url, e)
        return None
    if not r.ok or (r.content_type and "html" not in r.content_type):
        return None
    soup = soupify(r.text)
    if settings.lead_jina_fallback and len(_lines_text(soup)) < MIN_TEXT:
        rendered = _jina(settings, r.url)
        if rendered is not None:
            soup = rendered
    return r.url, soup


def _jina(settings: Settings, url: str) -> Optional[BeautifulSoup]:
    """Render a JavaScript-only page through Jina Reader (robots.txt was already checked by the fetcher)."""
    headers = {"X-Return-Format": "html", "User-Agent": settings.user_agent}
    if settings.jina_api_key:
        headers["Authorization"] = f"Bearer {settings.jina_api_key}"
    try:
        r = httpx.get(f"https://r.jina.ai/{url}", headers=headers, timeout=90)
        r.raise_for_status()
    except httpx.HTTPError as e:
        log.info("Jina Reader failed for %s: %s", url, e)
        return None
    return soupify(r.text)


def contact_links(soup: BeautifulSoup, page_url: str) -> list[str]:
    """Same-site links that look like contact / team / about pages, best first."""
    site = urlparse(page_url).netloc.lower().removeprefix("www.")
    ranked: list[tuple[int, str]] = []
    seen = {_page_key(page_url)}
    for a in soup.select("a[href]"):
        href = urljoin(page_url, a["href"]).split("#")[0]
        if not href.startswith("http") or urlparse(href).netloc.lower().removeprefix("www.") != site:
            continue
        path = urlparse(href).path
        if _page_key(href) in seen or NOT_CONTACT_PATH.search(path + "/") \
                or path.lower().endswith((".pdf", ".jpg", ".png", ".doc", ".docx")):
            continue
        hint = f"{path} {a.get_text(' ', strip=True)}"
        rank = next((i for i, rx in enumerate(CONTACT_LINK_HINTS) if rx.search(hint)), None)
        if rank is not None:
            seen.add(_page_key(href))
            ranked.append((rank, href))
    return [href for _, href in sorted(ranked, key=lambda x: x[0])]


def _page_key(url: str) -> str:
    """Same page regardless of http/https, www. or a trailing slash."""
    p = urlparse(url)
    return p.netloc.lower().removeprefix("www.") + p.path.rstrip("/").lower() + (f"?{p.query}" if p.query else "")


def emails_and_phones(soup: BeautifulSoup) -> tuple[list[str], list[str]]:
    emails, phones = [], []
    for a in soup.select("a[href]"):
        href = a["href"].strip()
        if href.lower().startswith("mailto:"):
            emails.append(href[7:].split("?")[0])
        elif href.lower().startswith("tel:"):
            phones.append(href[4:])
    text = _lines_text(soup)
    emails += EMAIL_RE.findall(text)
    phones += PHONE_RE.findall(text)
    emails = [e.strip(" .,;").lower() for e in emails]
    emails = [e for e in emails if "@" in e and len(e) < 80 and not JUNK_EMAIL.search(e)]
    phones = [p.strip() for p in phones if 10 <= len(re.sub(r"\D", "", p)) <= 13]
    return _merge([], emails), _merge([], phones, key=lambda p: re.sub(r"\D", "", p)[-10:])


def find_people(soup: BeautifulSoup) -> list[dict]:
    """Names next to a job title: 'Dr. A Sharma' on one line and 'Medical Director' on the line around it,
    or both on one line ('Dr. A Sharma, Medical Director')."""
    lines = [ln for ln in _lines(soup) if len(ln) <= MAX_LINE]
    people = []
    for i, line in enumerate(lines):
        role = ROLE_RE.search(line)
        if not role:
            continue
        name = _name_in(line[: role.start()].strip(" ,-–|:"))
        if not name:
            for j in (i - 1, i + 1):
                if 0 <= j < len(lines) and not ROLE_RE.search(lines[j]):
                    name = _name_in(lines[j], whole=True)
                    if name:
                        break
        if name:
            people.append({"name": name, "designation": line if name not in line else line[role.start():].strip()})
    return people


def _name_in(text: str, whole: bool = False) -> Optional[str]:
    text = text.strip(" ,-–|:")
    m = NAME_RE.fullmatch(text) if whole else NAME_RE.search(text)
    if not m:
        return None
    words = re.findall(r"[A-Za-z]+", m.group(0))
    if any(w.lower() in NOT_NAME_WORDS for w in words):
        return None
    return m.group(0)


PROMPT = """From the business web pages below, list the people who are named with a job title
(owners, directors, administrators, managers, heads of departments, purchase/procurement staff).
Return JSON: {{"people": [{{"name": str, "designation": str, "email": str or null, "phone": str or null}}]}}
Only include people and contact details that appear in the text. If none, return {{"people": []}}.

{pages}"""


def people_with_llm(settings: Settings, pages: list[tuple[str, BeautifulSoup]]) -> list[dict]:
    text = "\n\n".join(f"PAGE {url}:\n{_lines_text(soup)[:5000]}" for url, soup in pages)
    try:
        r = httpx.post(
            f"{settings.ollama_url.rstrip('/')}/api/generate",
            json={"model": settings.ollama_model, "prompt": PROMPT.format(pages=text),
                  "format": "json", "stream": False, "options": {"temperature": 0}},
            timeout=180,
        )
        r.raise_for_status()
        data = json.loads(r.json().get("response", "{}"))
    except Exception as e:
        log.warning("LLM people extraction failed: %s", e)
        return []
    people = data.get("people", []) if isinstance(data, dict) else []
    return [p for p in people if isinstance(p, dict) and p.get("name")]


def _lines(soup: BeautifulSoup) -> list[str]:
    """Visible text lines. Unlike extract.visible_text, keeps header/footer: that's where contacts usually are."""
    soup = BeautifulSoup(str(soup), "lxml")
    for tag in soup(["script", "style", "noscript", "svg", "template"]):
        tag.decompose()
    return [" ".join(ln.split()) for ln in soup.get_text("\n").splitlines() if ln.strip()]


def _lines_text(soup: BeautifulSoup) -> str:
    return "\n".join(_lines(soup))


def _merge(existing: list, new: list, key=lambda x: x) -> list:
    seen = {key(x) for x in existing}
    out = list(existing)
    for x in new:
        k = key(x)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out
