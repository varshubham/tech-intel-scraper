"""Download brochure PDFs, dedupe by hash, pull out text + contact/fee/date facts with regex."""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Optional

from ..core.config import Settings
from ..core.fetcher import Fetcher
from .dates import parse_date_range

log = logging.getLogger(__name__)

MAX_PDF_BYTES = 25 * 1024 * 1024
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE_RE = re.compile(r"(?:\+91[\s-]?)?(?:\(?0\d{2,4}\)?[\s-]?)?[6-9]\d{4}[\s-]?\d{5}|\+\d{1,3}[\s-]?\d{3,4}[\s-]?\d{3,4}[\s-]?\d{3,4}")
FEE_RE = re.compile(r"(?<![A-Za-z])(?:₹|Rs\.?|INR|USD|\$)\s?\d[\d,]+(?:\.\d+)?(?:\s?(?:/-|\+\s?GST))?", re.I)
DATE_CHUNK_RE = re.compile(
    r"\b\d{1,2}(?:st|nd|rd|th)?(?:\s*(?:-|–|to)\s*\d{1,2}(?:st|nd|rd|th)?)?\s+"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?,?\s+\d{4}",
    re.I,
)


def extract_pdf(content: bytes) -> tuple[str, int]:
    import fitz  # PyMuPDF

    with fitz.open(stream=content, filetype="pdf") as doc:
        text = "\n".join(page.get_text() for page in doc)
        return text, doc.page_count


def analyse_text(text: str) -> dict:
    flat = " ".join(text.split())
    dates = []
    for chunk in DATE_CHUNK_RE.findall(flat)[:10]:
        start, end = parse_date_range(chunk)
        if start:
            dates.append({"text": chunk, "start": start, "end": end})
    return {
        "emails": sorted(set(EMAIL_RE.findall(flat)))[:20],
        "phones": sorted({re.sub(r"\s+", " ", p.strip()) for p in PHONE_RE.findall(flat)})[:20],
        "fees": sorted(set(FEE_RE.findall(flat)))[:20],
        "dates": dates,
        "text_excerpt": flat[:2000],
    }


def download_brochure(fetcher: Fetcher, settings: Settings, url: str) -> Optional[dict]:
    try:
        r = fetcher.get(url, use_cache=False)
    except Exception as e:  # network errors, robots block
        log.info("Brochure fetch failed %s: %s", url, e)
        return None
    is_pdf = "pdf" in r.content_type.lower() or r.content[:5] == b"%PDF-"
    if not r.ok or not is_pdf:
        return None
    if len(r.content) > MAX_PDF_BYTES:
        log.info("Brochure too large, skipping: %s", url)
        return None

    sha = hashlib.sha256(r.content).hexdigest()
    path = settings.brochures_dir / f"{sha[:20]}.pdf"
    if not path.exists():
        path.write_bytes(r.content)

    info = {"url": url, "sha256": sha, "path": str(path.relative_to(settings.data_dir))}
    try:
        text, pages = extract_pdf(r.content)
        info["pages"] = pages
        if len(text.strip()) < 50:
            log.info("Brochure looks scanned (no text layer), OCR not run: %s", url)
        info.update(analyse_text(text))
    except Exception as e:
        log.warning("Could not parse PDF %s: %s", url, e)
    return info
