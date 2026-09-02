"""
WeatherClient -- every outbound HTTP call for weather, in one place.

Two responsibilities beyond fetching:

1. **Translation.** Every `requests` exception and every unhappy status code
   becomes an application exception with a sentence a person can read. Nothing
   above this layer ever sees a `requests` type.

2. **Resilience.** Responses are cached for 30 minutes; when the network is
   down, an expired cache entry is served rather than nothing, flagged so the
   UI can say so.
"""

from __future__ import annotations

import logging

import requests

import config
from core.exceptions import (
    APIError,
    ConnectionFailedError,
    LocationNotFoundError,
    MalformedResponseError,
    NetworkError,
    RateLimitError,
    RequestTimeoutError,
    ServiceUnavailableError,
)
from core.models import Forecast, Location
from core.validators import clean_location, parse_coordinates

log = logging.getLogger(__name__)


class WeatherClient:
    """Geocoding and forecasts from Open-Meteo. No API key required."""

    def __init__(
        self,
        storage=None,
        *,
        timeout: int | None = None,
        retries: int | None = None,
        session=None,
    ):
        self.storage = storage
        self.timeout = timeout if timeout is not None else config.REQUEST_TIMEOUT
        self.retries = retries if retries is not None else config.REQUEST_RETRIES
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "WeatherRiskPlanner/1.0")

    # -----------------------------------------------------------------------
    # Geocoding
    # -----------------------------------------------------------------------
    def geocode(self, query: str) -> list[Location]:
        """
        Turn typed text into candidate locations, best match first.

        A coordinate pair skips the network entirely. Anything else is cleaned
        by regex first, so the API is never asked about 'ib@adan!!'.
        """
        coords = parse_coordinates(query)
        if coords:
            latitude, longitude = coords
            return [
                Location(
                    name=f"{latitude:.4f}, {longitude:.4f}",
                    latitude=latitude,
                    longitude=longitude,
                )
            ]

        name = clean_location(query)
        payload = self._request(
            config.GEOCODE_URL,
            {"name": name, "count": config.GEOCODE_RESULTS, "language": "en", "format": "json"},
        )

        # A place that does not exist is NOT an HTTP error: the API answers 200
        # with the "results" key absent entirely.
        results = payload.get("results")
        if not results:
            raise LocationNotFoundError(
                f"No place called {name!r} was found.",
                hint="Check the spelling, or try adding the country: 'Ibadan, Nigeria'",
            )

        locations: list[Location] = []
        for item in results:
            try:
                locations.append(
                    Location(
                        name=item["name"],
                        latitude=float(item["latitude"]),
                        longitude=float(item["longitude"]),
                        country=item.get("country", "") or "",
                        admin1=item.get("admin1", "") or "",
                        timezone=item.get("timezone", "auto") or "auto",
                    )
                )
            except (KeyError, TypeError, ValueError) as exc:
                log.warning("Skipping malformed geocoding result %r: %s", item, exc)

        if not locations:
            raise MalformedResponseError(
                "The location service returned data the app could not read.",
                hint="Try a different spelling, or try again shortly.",
            )
        return locations

    # -----------------------------------------------------------------------
    # Forecast
    # -----------------------------------------------------------------------
    def fetch_forecast(self, location: Location, days: int | None = None) -> Forecast:
        """
        Cache-first, then network, then stale cache.

        The third step is what keeps the app usable on a dropped connection:
        an old forecast marked `from_cache` beats an error dialog.
        """
        days = days or config.FORECAST_DAYS
        key = location.key()

        if self.storage:
            fresh = self.storage.cache_get(key)
            if fresh:
                payload, fetched_at = fresh
                log.info("Serving cached forecast for %s", location.display_name)
                return Forecast.from_api(
                    payload, location, fetched_at=fetched_at, from_cache=True
                )

        params = {
            "latitude": location.latitude,
            "longitude": location.longitude,
            "hourly": config.HOURLY_FIELDS,
            "daily": config.DAILY_FIELDS,
            "timezone": "auto",
            "forecast_days": days,
            "wind_speed_unit": "kmh",
        }

        try:
            payload = self._request(config.FORECAST_URL, params)
        except (NetworkError, APIError) as exc:
            stale = self.storage.cache_get(key, allow_stale=True) if self.storage else None
            if stale:
                cached_payload, fetched_at = stale
                log.warning(
                    "Live forecast failed (%s); serving cache from %s", exc, fetched_at
                )
                return Forecast.from_api(
                    cached_payload, location, fetched_at=fetched_at, from_cache=True
                )
            raise

        if self.storage:
            self.storage.cache_put(key, payload)
        return Forecast.from_api(payload, location)

    # -----------------------------------------------------------------------
    # The single place where network failure is translated
    # -----------------------------------------------------------------------
    def _request(self, url: str, params: dict) -> dict:
        """
        GET with timeout, bounded retries and full error translation.

        Retries cover timeouts, connection errors and 5xx only. A 4xx is a bad
        request -- retrying it just makes the user wait longer for the same
        answer.
        """
        last_error: Exception | None = None

        for attempt in range(self.retries + 1):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.Timeout as exc:
                last_error = exc
                if attempt < self.retries:
                    self._backoff(attempt)
                    continue
                raise RequestTimeoutError(
                    "The weather service is slow to respond.",
                    hint="Check your connection and try again.",
                    cause=exc,
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                last_error = exc
                if attempt < self.retries:
                    self._backoff(attempt)
                    continue
                raise ConnectionFailedError(
                    "Can't reach the weather service.",
                    hint="You appear to be offline.",
                    cause=exc,
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise NetworkError(
                    "The weather request could not be sent.", cause=exc
                ) from exc

            status = response.status_code
            if status == 429:
                raise RateLimitError(
                    "Too many requests just now — try again in a minute.",
                    hint="The free weather service limits how often it can be called.",
                )
            if status >= 500:
                last_error = APIError(f"Weather service returned {status}.")
                if attempt < self.retries:
                    self._backoff(attempt)
                    continue
                raise ServiceUnavailableError(
                    "The weather service is temporarily unavailable.",
                    hint=f"It returned error {status}. Try again shortly.",
                )
            if status >= 400:
                reason = self._error_reason(response)
                raise APIError(
                    "The weather service rejected that request.",
                    hint=reason or f"HTTP {status}",
                )

            try:
                payload = response.json()
            except ValueError as exc:
                raise MalformedResponseError(
                    "The weather service sent a reply the app could not read.",
                    hint="Try again in a moment.",
                    cause=exc,
                ) from exc

            if not isinstance(payload, dict):
                raise MalformedResponseError(
                    "The weather service sent an unexpected reply.",
                    hint="Try again in a moment.",
                )
            return payload

        # Unreachable in practice; kept so no path returns None silently.
        raise NetworkError("The weather request failed.", cause=last_error)

    @staticmethod
    def _backoff(attempt: int) -> None:
        import time

        time.sleep(0.5 if attempt == 0 else 1.5)

    @staticmethod
    def _error_reason(response) -> str:
        try:
            body = response.json()
        except ValueError:
            return ""
        if isinstance(body, dict):
            return str(body.get("reason") or body.get("error") or "")
        return ""
