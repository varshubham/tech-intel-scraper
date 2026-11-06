"""All settings come from environment variables so the same image runs anywhere."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _float_or_none(name: str) -> Optional[float]:
    val = os.getenv(name, "").strip()
    return float(val) if val else None


def _list(val: str) -> list:
    return [v.strip().lower() for v in val.split(",") if v.strip()]


def _load_dotenv() -> None:
    """Minimal .env loader (no extra dependency). Real env vars take precedence."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if value[:1] in ('"', "'"):
            value = value[1:].split(value[0], 1)[0]
        else:
            value = value.split(" #", 1)[0].strip()  # inline comment
        os.environ.setdefault(key.strip(), value)


@dataclass
class Settings:
    data_dir: Path
    sources_file: Path
    keywords_file: Path
    user_agent: str
    request_delay: float
    request_timeout: float
    respect_robots: bool
    cache_ttl_hours: float
    use_playwright: bool
    use_llm: bool
    ollama_url: str
    ollama_model: str
    max_detail_pages: int
    download_brochures: bool
    crawl_interval_hours: float
    api_token: str
    host: str
    port: int
    lead_lat: Optional[float]
    lead_lng: Optional[float]
    lead_radius_km: float
    lead_types: list
    lead_max_businesses: int
    lead_max_pages: int
    lead_jina_fallback: bool
    google_places_api_key: str
    jina_api_key: str
    keywords: dict = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "events.db"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def brochures_dir(self) -> Path:
        return self.data_dir / "brochures"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"


def load_settings() -> Settings:
    _load_dotenv()
    s = Settings(
        data_dir=Path(os.getenv("DATA_DIR", ROOT / "data")),
        sources_file=Path(os.getenv("SOURCES_FILE", ROOT / "config" / "sources.yaml")),
        keywords_file=Path(os.getenv("KEYWORDS_FILE", ROOT / "config" / "keywords.yaml")),
        user_agent=os.getenv(
            "USER_AGENT",
            "Mozilla/5.0 (compatible; TechEventsBot/1.0; +https://example.com/bot)",
        ),
        request_delay=float(os.getenv("REQUEST_DELAY", "3")),
        request_timeout=float(os.getenv("REQUEST_TIMEOUT", "30")),
        respect_robots=_bool("RESPECT_ROBOTS", True),
        cache_ttl_hours=float(os.getenv("CACHE_TTL_HOURS", "12")),
        use_playwright=_bool("USE_PLAYWRIGHT", False),
        use_llm=_bool("USE_LLM", False),
        ollama_url=os.getenv("OLLAMA_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:7b"),
        max_detail_pages=int(os.getenv("MAX_DETAIL_PAGES", "30")),
        download_brochures=_bool("DOWNLOAD_BROCHURES", True),
        crawl_interval_hours=float(os.getenv("CRAWL_INTERVAL_HOURS", "24")),
        api_token=os.getenv("API_TOKEN", ""),
        host=os.getenv("HOST", "0.0.0.0"),
        # PORT is what Render / Railway / Fly / Heroku inject
        port=int(os.getenv("PORT", "8000")),
        lead_lat=_float_or_none("LEAD_LAT"),
        lead_lng=_float_or_none("LEAD_LNG"),
        lead_radius_km=float(os.getenv("LEAD_RADIUS_KM", "6")),
        lead_types=_list(os.getenv("LEAD_TYPES", "hospital")),
        lead_max_businesses=int(os.getenv("LEAD_MAX_BUSINESSES", "100")),
        lead_max_pages=int(os.getenv("LEAD_MAX_PAGES", "4")),
        lead_jina_fallback=_bool("LEAD_JINA_FALLBACK", True),
        google_places_api_key=os.getenv("GOOGLE_PLACES_API_KEY", ""),
        jina_api_key=os.getenv("JINA_API_KEY", ""),
    )
    for d in (s.data_dir, s.cache_dir, s.brochures_dir, s.exports_dir):
        d.mkdir(parents=True, exist_ok=True)
    s.keywords = yaml.safe_load(s.keywords_file.read_text()) if s.keywords_file.exists() else {}
    return s


def load_sources(settings: Settings) -> list[dict]:
    data = yaml.safe_load(settings.sources_file.read_text()) or {}
    return [src for src in data.get("sources", []) if src.get("enabled", True)]
