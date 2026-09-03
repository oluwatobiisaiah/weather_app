"""Parsing the API shape, surviving holes in it, and round-tripping to disk."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from conftest import make_payload

from core.exceptions import MissingDataError
from core.models import Forecast, Location, RiskBand, describe_code


@pytest.fixture
def location() -> Location:
    return Location(name="Ibadan", latitude=7.3776, longitude=3.9059, country="Nigeria")


class TestFromApi:
    def test_zips_the_parallel_arrays(self, payload, location):
        forecast = Forecast.from_api(payload, location)
        assert len(forecast.hours) == 24
        first = forecast.hours[0]
        assert first.time == datetime.combine(date.today(), datetime.min.time())
        assert first.temperature_c == 22.0
        assert first.description == "Clear sky"

    def test_marks_night_hours(self, payload, location):
        forecast = Forecast.from_api(payload, location)
        assert forecast.hours[3].is_daylight is False
        assert forecast.hours[12].is_daylight is True

    def test_reads_the_daily_summary(self, payload, location):
        forecast = Forecast.from_api(payload, location)
        summary = forecast.day(date.today())
        assert summary is not None
        assert summary.sunrise and summary.sunrise.hour == 6
        assert summary.sunset and summary.sunset.hour == 18

    def test_a_null_is_a_hole_not_a_crash(self, location):
        payload = make_payload(overrides={5: {"uv_index": None, "wind_speed_10m": None}})
        forecast = Forecast.from_api(payload, location)
        assert forecast.hours[5].uv is None
        assert forecast.hours[5].wind_kmh is None
        assert forecast.hours[5].temperature_c == 22.0  # the rest still parsed

    def test_a_short_series_is_padded(self, payload, location):
        payload["hourly"]["uv_index"] = [1.0, 2.0]  # server sent fewer values
        forecast = Forecast.from_api(payload, location)
        assert forecast.hours[0].uv == 1.0
        assert forecast.hours[10].uv is None

    def test_missing_time_series_is_fatal(self, location):
        with pytest.raises(MissingDataError):
            Forecast.from_api({"hourly": {}}, location)

    def test_timezone_from_the_response_wins(self, payload, location):
        forecast = Forecast.from_api(payload, location)
        assert forecast.location.timezone == "Africa/Lagos"


class TestRoundTrip:
    def test_to_dict_then_from_dict_is_equal(self, payload, location):
        original = Forecast.from_api(payload, location)
        restored = Forecast.from_dict(original.to_dict())

        assert len(restored.hours) == len(original.hours)
        assert restored.location.name == original.location.name
        assert restored.fetched_at == original.fetched_at
        for before, after in zip(original.hours, restored.hours):
            assert before == after


class TestQueries:
    def test_hours_for_date_filters(self, location):
        from datetime import timedelta

        forecast = Forecast.from_api(make_payload(hours=48), location)
        assert len(forecast.hours) == 48
        assert len(forecast.hours_for_date(date.today())) == 24
        assert len(forecast.hours_for_date(date.today() + timedelta(days=1))) == 24
        assert forecast.hours_for_date(date(2000, 1, 1)) == []

    def test_available_dates_are_unique_and_ordered(self, payload, location):
        from datetime import timedelta

        forecast = Forecast.from_api(payload, location)
        assert forecast.available_dates() == [date.today()]

        two_days = Forecast.from_api(make_payload(hours=48), location)
        assert two_days.available_dates() == [
            date.today(),
            date.today() + timedelta(days=1),
        ]

    def test_staleness_uses_fetched_at(self, payload, location):
        forecast = Forecast.from_api(
            payload, location, fetched_at=datetime(2020, 1, 1, 12, 0)
        )
        assert forecast.is_stale(minutes=30) is True
        assert forecast.age_minutes > 0


class TestRiskBand:
    @pytest.mark.parametrize(
        "score, band",
        [
            (0, RiskBand.SAFE),
            (24.9, RiskBand.SAFE),
            (25, RiskBand.MANAGEABLE),
            (49.9, RiskBand.MANAGEABLE),
            (50, RiskBand.RISKY),
            (74.9, RiskBand.RISKY),
            (75, RiskBand.AVOID),
            (100, RiskBand.AVOID),
        ],
    )
    def test_boundaries(self, score, band):
        assert RiskBand.from_score(score) is band

    def test_every_band_has_a_colour_and_a_sentence(self):
        for band in RiskBand:
            assert band.colour.startswith("#")
            assert band.verdict_sentence


class TestCodes:
    def test_known_code(self):
        assert describe_code(95) == "Thunderstorm"

    def test_unknown_code_does_not_crash(self):
        assert "77777" in describe_code(77777)

    def test_none_is_unknown(self):
        assert describe_code(None) == "Unknown"
