"""Optional fallback: ask a local Ollama model to pull events out of page text.
Only used when USE_LLM=true and the rule-based extractors found nothing."""
from __future__ import annotations

import json
import logging

import httpx

from ..core.config import Settings
from .dates import parse_date

log = logging.getLogger(__name__)

PROMPT = """Extract every upcoming event from the web page text below.
Return JSON: {{"events": [{{"title": str, "start_date": "YYYY-MM-DD" or null, "end_date": "YYYY-MM-DD" or null,
"city": str or null, "venue": str or null, "mode": "online"|"offline"|"hybrid"|null,
"organizer": str or null, "description": str or null}}]}}
Only include real events that appear in the text. If none, return {{"events": []}}.

PAGE URL: {url}
PAGE TEXT:
{text}"""


def extract_with_llm(settings: Settings, text: str, url: str) -> list[dict]:
    try:
        r = httpx.post(
            f"{settings.ollama_url.rstrip('/')}/api/generate",
            json={
                "model": settings.ollama_model,
                "prompt": PROMPT.format(url=url, text=text),
                "format": "json",
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=180,
        )
        r.raise_for_status()
        data = json.loads(r.json().get("response", "{}"))
    except Exception as e:
        log.warning("LLM extraction failed for %s: %s", url, e)
        return []

    events = []
    for e in data.get("events", []) if isinstance(data, dict) else []:
        if not isinstance(e, dict) or not e.get("title"):
            continue
        e["start_date"] = parse_date(e.get("start_date"))
        e["end_date"] = parse_date(e.get("end_date"))
        events.append(e)
    return events
