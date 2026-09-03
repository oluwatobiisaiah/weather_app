"""
Network behaviour, without a network.

A fake session stands in for requests.Session, so every failure mode -- the
200-with-no-results trap, timeouts, 5xx, 429, junk bodies -- is reproducible.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
import requests
from conftest import make_payload

from core.exceptions import (
    APIError,
    ConnectionFailedError,
    InvalidLocationError,
    LocationNotFoundError,
    MalformedResponseError,
    RateLimitError,
    RequestTimeoutError,
    ServiceUnavailableError,
)
from core.models import Location
from core.weather_client import WeatherClient

GEOCODE_HIT = {
    "results": [
        {
            "name": "Ibadan",
            "latitude": 7.37756,
            "longitude": 3.90591,
            "country": "Nigeria",
            "admin1": "Oyo",
            "timezone": "Africa/Lagos",
        }
    ]
}


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None):
        self.status_code = status_code
        self._payload = payload
        self._text = text

    def json(self):
        if self._text is not None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Returns queued responses, or raises queued exceptions, in order."""

    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = 0
        self.headers = {}
        self.last_params = None

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        self.last_params = params
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    """Retries are real; the waiting between them is not, in tests."""
    monkeypatch.setattr(WeatherClient, "_backoff", staticmethod(lambda _attempt: None))


@pytest.fixture
def location() -> Location:
    return Location(name="Ibadan", latitude=7.3776, longitude=3.9059)


class TestGeocode:
    def test_returns_locations(self):
        session = FakeSession(FakeResponse(payload=GEOCODE_HIT))
        client = WeatherClient(session=session)
        results = client.geocode("ibadan")
        assert results[0].name == "Ibadan"
        assert results[0].country == "Nigeria"
        assert session.last_params["name"] == "Ibadan"  # cleaned before sending

    def test_200_with_no_results_is_location_not_found(self):
        """The trap: a missing place is not an HTTP error."""
        session = FakeSession(FakeResponse(payload={"generationtime_ms": 0.3}))
        client = WeatherClient(session=session)
        with pytest.raises(LocationNotFoundError) as info:
            client.geocode("Lagoss")
        assert "Lagoss" in info.value.user_message

    def test_junk_input_never_reaches_the_network(self):
        session = FakeSession(FakeResponse(payload=GEOCODE_HIT))
        client = WeatherClient(session=session)
        with pytest.raises(InvalidLocationError):
            client.geocode("!!!")
        assert session.calls == 0

    def test_coordinates_skip_the_network(self):
        session = FakeSession(FakeResponse(payload=GEOCODE_HIT))
        client = WeatherClient(session=session)
        results = client.geocode("7.3776, 3.9059")
        assert session.calls == 0
        assert results[0].latitude == 7.3776

    def test_malformed_results_are_reported(self):
        session = FakeSession(FakeResponse(payload={"results": [{"nope": 1}]}))
        client = WeatherClient(session=session)
        with pytest.raises(MalformedResponseError):
            client.geocode("Ibadan")


class TestRetryPolicy:
    def test_timeout_is_retried_then_reported(self):
        session = FakeSession(requests.exceptions.Timeout("slow"))
        client = WeatherClient(session=session, retries=2)
        with pytest.raises(RequestTimeoutError):
            client.geocode("Ibadan")
        assert session.calls == 3  # first attempt + 2 retries

    def test_connection_error_is_retried_then_reported(self):
        session = FakeSession(requests.exceptions.ConnectionError("offline"))
        client = WeatherClient(session=session, retries=1)
        with pytest.raises(ConnectionFailedError) as info:
            client.geocode("Ibadan")
        assert session.calls == 2
        assert "offline" in info.value.hint.lower()

    def test_server_error_is_retried(self):
        session = FakeSession(FakeResponse(status_code=503))
        client = WeatherClient(session=session, retries=2)
        with pytest.raises(ServiceUnavailableError):
            client.geocode("Ibadan")
        assert session.calls == 3

    def test_a_recovering_service_succeeds_on_the_retry(self):
        session = FakeSession(
            FakeResponse(status_code=500), FakeResponse(payload=GEOCODE_HIT)
        )
        client = WeatherClient(session=session, retries=2)
        assert client.geocode("Ibadan")[0].name == "Ibadan"
        assert session.calls == 2

    def test_client_error_is_not_retried(self):
        session = FakeSession(FakeResponse(status_code=404, payload={"reason": "bad"}))
        client = WeatherClient(session=session, retries=2)
        with pytest.raises(APIError):
            client.geocode("Ibadan")
        assert session.calls == 1

    def test_rate_limit_is_not_retried(self):
        session = FakeSession(FakeResponse(status_code=429))
        client = WeatherClient(session=session, retries=2)
        with pytest.raises(RateLimitError):
            client.geocode("Ibadan")
        assert session.calls == 1

    def test_non_json_body_is_reported(self):
        session = FakeSession(FakeResponse(text="<html>maintenance</html>"))
        client = WeatherClient(session=session)
        with pytest.raises(MalformedResponseError):
            client.geocode("Ibadan")


class TestForecastAndCache:
    def test_fetch_stores_in_the_cache(self, storage, location):
        payload = make_payload()
        session = FakeSession(FakeResponse(payload=payload))
        client = WeatherClient(storage, session=session)

        forecast = client.fetch_forecast(location)
        assert forecast.from_cache is False
        assert len(forecast.hours) == 24
        assert storage.cache_get(location.key()) is not None

    def test_second_call_is_served_from_the_cache(self, storage, location):
        session = FakeSession(FakeResponse(payload=make_payload()))
        client = WeatherClient(storage, session=session)

        client.fetch_forecast(location)
        second = client.fetch_forecast(location)
        assert session.calls == 1  # no second network call
        assert second.from_cache is True

    def test_offline_falls_back_to_a_stale_cache(self, storage, location):
        storage.cache_put(location.key(), make_payload())
        # age it past the TTL
        raw = json.loads(storage._cache_path(location.key()).read_text(encoding="utf-8"))
        raw["fetched_at"] = (datetime.now() - timedelta(hours=8)).isoformat()
        storage._cache_path(location.key()).write_text(json.dumps(raw), encoding="utf-8")

        session = FakeSession(requests.exceptions.ConnectionError("offline"))
        client = WeatherClient(storage, session=session, retries=0)

        forecast = client.fetch_forecast(location)
        assert forecast.from_cache is True
        assert forecast.is_stale(minutes=30) is True

    def test_offline_with_no_cache_raises(self, storage, location):
        session = FakeSession(requests.exceptions.ConnectionError("offline"))
        client = WeatherClient(storage, session=session, retries=0)
        with pytest.raises(ConnectionFailedError):
            client.fetch_forecast(location)

    def test_requests_the_fields_the_analyzer_needs(self, storage, location):
        session = FakeSession(FakeResponse(payload=make_payload()))
        client = WeatherClient(storage, session=session)
        client.fetch_forecast(location)

        hourly = session.last_params["hourly"]
        for field in (
            "apparent_temperature", "precipitation_probability", "weather_code",
            "wind_gusts_10m", "uv_index", "visibility", "is_day",
        ):
            assert field in hourly
        assert session.last_params["timezone"] == "auto"
        assert session.last_params["wind_speed_unit"] == "kmh"
