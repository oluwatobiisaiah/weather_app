"""
Central configuration for the Weather Risk & Outdoor Activity Planner.

Everything that the rest of the app needs to know about *where things live* and
*which knobs can be turned* is defined here, once:

  * folder and file paths, all anchored to this file rather than to the current
    working directory, so the app works when launched by double-click, from an
    IDE, or from any other folder;
  * the two API endpoints;
  * tunables that may be overridden from a `.env` file;
  * logging setup (console + a rotating file).
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths -- always relative to THIS file, never to os.getcwd()
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PLANS_DIR = DATA_DIR / "plans"
CACHE_DIR = DATA_DIR / "cache"
LOGS_DIR = BASE_DIR / "logs"

ACTIVITIES_FILE = DATA_DIR / "activities.json"
FAVOURITES_FILE = DATA_DIR / "favourites.json"
HISTORY_FILE = DATA_DIR / "search_history.json"
LOG_FILE = LOGS_DIR / "app.log"
ENV_FILE = BASE_DIR / ".env"

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

HOURLY_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,"
    "precipitation_probability,precipitation,weather_code,"
    "wind_speed_10m,wind_gusts_10m,uv_index,visibility,cloud_cover,is_day"
)
DAILY_FIELDS = (
    "weather_code,temperature_2m_max,temperature_2m_min,"
    "precipitation_sum,precipitation_probability_max,"
    "wind_speed_10m_max,uv_index_max,sunrise,sunset"
)

# ---------------------------------------------------------------------------
# Fixed limits
# ---------------------------------------------------------------------------
FORECAST_DAYS = 7
HISTORY_LIMIT = 50
FAVOURITES_LIMIT = 30
GEOCODE_RESULTS = 5

APP_NAME = "Weather Risk & Outdoor Activity Planner"


# ---------------------------------------------------------------------------
# .env loading
#
# python-dotenv is used when it happens to be installed, but the app must not
# fall over without it -- a six-line parser covers the KEY=value case we need.
# ---------------------------------------------------------------------------
def load_env(path: Path = ENV_FILE) -> None:
    """Read `path` into os.environ without overwriting real environment vars."""
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv(path, override=False)
        return
    except ImportError:
        pass

    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    except OSError:
        # A missing or unreadable .env is never fatal: the app runs without AI.
        pass


load_env()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Tunables (overridable from .env)
# ---------------------------------------------------------------------------
CACHE_MINUTES = _env_int("FORECAST_CACHE_MINUTES", 30)
REQUEST_TIMEOUT = _env_int("REQUEST_TIMEOUT_SECONDS", 10)
REQUEST_RETRIES = _env_int("REQUEST_RETRIES", 2)
AI_TIMEOUT = _env_int("AI_TIMEOUT_SECONDS", 20)

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def gemini_api_key() -> str:
    """Read the key at call time so a `.env` edit does not need a restart."""
    return os.environ.get("GEMINI_API_KEY", "").strip()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def ensure_directories() -> None:
    """Create every folder the app writes to. Safe to call repeatedly."""
    for folder in (DATA_DIR, PLANS_DIR, CACHE_DIR, LOGS_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def setup_logging(level: int = logging.INFO) -> logging.Logger:
    """Configure the root logger once: console at WARNING, file at `level`."""
    ensure_directories()
    root = logging.getLogger()
    if getattr(root, "_planner_configured", False):
        return logging.getLogger("weather_app")

    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")

    try:
        file_handler = RotatingFileHandler(
            LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        file_handler.setLevel(level)
        root.addHandler(file_handler)
    except OSError:
        # Read-only disk should not stop the app from running.
        pass

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    console.setLevel(logging.WARNING)
    root.addHandler(console)

    root._planner_configured = True  # type: ignore[attr-defined]
    return logging.getLogger("weather_app")
