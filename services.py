"""
One place that builds the object graph, used by both the GUI and the CLI.

Composition happens here so neither entry point has to know how the pieces fit
together, and so a test can build the same graph pointed at a temp folder.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import config
from core.ai_client import GeminiClient
from core.recommendation import RecommendationEngine
from core.risk_analyzer import ActivityRiskAnalyzer
from core.storage import StorageManager
from core.weather_client import WeatherClient

log = logging.getLogger(__name__)


@dataclass
class Services:
    storage: StorageManager
    client: WeatherClient
    analyzer: ActivityRiskAnalyzer
    engine: RecommendationEngine
    profiles: dict
    ai: GeminiClient | None

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai and self.ai.available)


def build_services(data_dir: Path | None = None, *, use_ai: bool = True) -> Services:
    """Create every service the app needs, wired together."""
    config.ensure_directories()

    storage = StorageManager(data_dir)
    profiles = storage.load_activities()
    analyzer = ActivityRiskAnalyzer(profiles)
    client = WeatherClient(storage)

    ai: GeminiClient | None = None
    if use_ai:
        ai = GeminiClient()
        if not ai.available:
            log.info("No GEMINI_API_KEY set — running with rule-based wording.")
        elif not ai.key_looks_valid():
            log.warning(
                "GEMINI_API_KEY does not look like a Google API key; the first call may fail."
            )

    engine = RecommendationEngine(analyzer, profiles, ai_client=ai)
    return Services(
        storage=storage,
        client=client,
        analyzer=analyzer,
        engine=engine,
        profiles=profiles,
        ai=ai,
    )
