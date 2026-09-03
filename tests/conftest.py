"""
Shared fixtures. The suite never touches the network: every test drives the app
from a hand-built payload, which is exactly why Forecast was kept dumb.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

# The app is run as scripts from its own folder, so tests put that folder on the
# path the same way `python main.py` does.
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.models import ActivityProfile  # noqa: E402
from core.recommendation import RecommendationEngine  # noqa: E402
from core.risk_analyzer import ActivityRiskAnalyzer  # noqa: E402
from core.storage import StorageManager  # noqa: E402


def make_payload(
    *,
    day: date | None = None,
    hours: int = 24,
    temp: float = 22.0,
    feels: float | None = None,
    humidity: float = 55,
    prob: float = 5,
    precip: float = 0.0,
    code: int = 0,
    wind: float = 8.0,
    gust: float = 14.0,
    uv: float = 3.0,
    visibility: float = 20000,
    cloud: float = 20,
    overrides: dict[int, dict] | None = None,
) -> dict:
    """
    Build an Open-Meteo-shaped payload.

    `overrides` patches individual hours: {14: {"temperature_2m": 38}}.
    """
    day = day or date.today()
    feels = temp if feels is None else feels
    start = datetime.combine(day, datetime.min.time())

    rows = []
    for i in range(hours):
        row = {
            # Built by arithmetic, not string formatting, so hours past 23
            # roll into the next day instead of producing an invalid "T24:00".
            "time": (start + timedelta(hours=i)).isoformat(timespec="minutes"),
            "temperature_2m": temp,
            "apparent_temperature": feels,
            "relative_humidity_2m": humidity,
            "precipitation_probability": prob,
            "precipitation": precip,
            "weather_code": code,
            "wind_speed_10m": wind,
            "wind_gusts_10m": gust,
            "uv_index": uv,
            "visibility": visibility,
            "cloud_cover": cloud,
            "is_day": 1 if 6 <= (i % 24) <= 18 else 0,
        }
        row.update((overrides or {}).get(i, {}))
        rows.append(row)

    hourly = {key: [r[key] for r in rows] for key in rows[0]}

    day_count = max(1, -(-hours // 24))  # ceiling division
    days = [day + timedelta(days=i) for i in range(day_count)]
    return {
        "timezone": "Africa/Lagos",
        "hourly": hourly,
        "daily": {
            "time": [d.isoformat() for d in days],
            "weather_code": [code] * day_count,
            "temperature_2m_max": [temp + 4] * day_count,
            "temperature_2m_min": [temp - 4] * day_count,
            "precipitation_sum": [precip * 24] * day_count,
            "precipitation_probability_max": [prob] * day_count,
            "wind_speed_10m_max": [wind] * day_count,
            "uv_index_max": [uv] * day_count,
            "sunrise": [f"{d.isoformat()}T06:20" for d in days],
            "sunset": [f"{d.isoformat()}T18:45" for d in days],
        },
    }


@pytest.fixture
def payload():
    """A pleasant, unremarkable day."""
    return make_payload()


@pytest.fixture
def storm_payload():
    """The same day with a thunderstorm parked over the afternoon."""
    return make_payload(
        temp=26.0,
        feels=29.0,
        overrides={
            h: {"weather_code": 95, "precipitation_probability": 90, "precipitation": 8.0}
            for h in (14, 15, 16)
        },
    )


@pytest.fixture
def profiles() -> dict[str, ActivityProfile]:
    raw = json.loads((BASE_DIR / "data" / "activities.json").read_text(encoding="utf-8"))
    return {key: ActivityProfile.from_dict(key, value) for key, value in raw.items()}


@pytest.fixture
def analyzer(profiles) -> ActivityRiskAnalyzer:
    return ActivityRiskAnalyzer(profiles)


@pytest.fixture
def engine(analyzer, profiles) -> RecommendationEngine:
    return RecommendationEngine(analyzer, profiles, ai_client=None)


@pytest.fixture
def storage(tmp_path) -> StorageManager:
    """A StorageManager pointed at a throwaway folder, with real profiles."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "activities.json").write_text(
        (BASE_DIR / "data" / "activities.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    return StorageManager(data_dir)


@pytest.fixture
def today() -> date:
    return date.today()


@pytest.fixture
def tomorrow() -> date:
    return date.today() + timedelta(days=1)
