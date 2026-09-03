"""
The scoring rules -- the part of the app that is allowed to be authoritative,
and therefore the part with the most tests.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from conftest import make_payload

from core.models import Forecast, HourPoint, Location, RiskBand
from core.risk_analyzer import ramp_down, ramp_up


@pytest.fixture
def location() -> Location:
    return Location(name="Ibadan", latitude=7.3776, longitude=3.9059)


def hour(**kwargs) -> HourPoint:
    """A pleasant hour, patched by whatever the test cares about."""
    defaults = dict(
        time=datetime.combine(date.today(), datetime.min.time()).replace(hour=10),
        temperature_c=22.0,
        apparent_c=22.0,
        humidity=50.0,
        precip_mm=0.0,
        precip_prob=5.0,
        weather_code=0,
        wind_kmh=8.0,
        gust_kmh=14.0,
        uv=3.0,
        visibility_m=20000.0,
        cloud_cover=10.0,
        is_day=True,
    )
    defaults.update(kwargs)
    return HourPoint(**defaults)


class TestRamps:
    def test_zero_inside_ideal(self):
        assert ramp_up(20, 26, 33) == 0

    def test_hundred_beyond_limit(self):
        assert ramp_up(40, 26, 33) == 100

    def test_linear_in_between(self):
        assert ramp_up(29.5, 26, 33) == pytest.approx(50)

    def test_downward_ramp(self):
        assert ramp_down(5000, 5000, 500) == 0
        assert ramp_down(500, 5000, 500) == 100
        assert ramp_down(2750, 5000, 500) == pytest.approx(50)

    def test_degenerate_bounds_do_not_divide_by_zero(self):
        assert ramp_up(10, 5, 5) == 100
        assert ramp_down(10, 5, 5) == 0


class TestPleasantConditions:
    def test_a_perfect_hour_is_safe_for_every_activity(self, analyzer, profiles):
        for key, profile in profiles.items():
            assessment = analyzer.assess_hour(hour(), profile)
            assert assessment.score < 10, f"{key} scored {assessment.score}"
            assert assessment.band is RiskBand.SAFE

    def test_no_hard_stops_on_a_good_day(self, analyzer, profiles):
        assessment = analyzer.assess_hour(hour(), profiles["football"])
        assert assessment.hard_stops == []
        assert assessment.reasons == []


class TestHardStops:
    def test_thunderstorm_forces_avoid(self, analyzer, profiles):
        for key, profile in profiles.items():
            assessment = analyzer.assess_hour(hour(weather_code=95), profile)
            assert assessment.band is RiskBand.AVOID, key
            assert "thunderstorm" in assessment.hard_stops
            assert any("lightning" in r.lower() for r in assessment.reasons)

    def test_extreme_gusts_floor_the_score(self, analyzer, profiles):
        # football limit is 40 km/h, so the stop trips above 60
        assessment = analyzer.assess_hour(hour(gust_kmh=70), profiles["football"])
        assert assessment.score >= 75
        assert "gusts" in assessment.hard_stops

    def test_extreme_heat_floors_the_score(self, analyzer, profiles):
        assessment = analyzer.assess_hour(
            hour(temperature_c=38, apparent_c=44), profiles["farming"]
        )
        assert assessment.score >= 75
        assert "extreme_heat" in assessment.hard_stops

    def test_extreme_cold_floors_the_score(self, analyzer, profiles):
        assessment = analyzer.assess_hour(
            hour(temperature_c=-6, apparent_c=-9), profiles["travelling"]
        )
        assert assessment.score >= 75
        assert "extreme_cold" in assessment.hard_stops


class TestActivitiesDiffer:
    def test_rain_hurts_a_picnic_more_than_farming(self, analyzer, profiles):
        wet = hour(precip_prob=80, precip_mm=3.0, weather_code=63)
        picnic = analyzer.assess_hour(wet, profiles["picnic"])
        farming = analyzer.assess_hour(wet, profiles["farming"])
        assert picnic.score > farming.score
        assert picnic.factors["rain"] > farming.factors["rain"]

    def test_fog_hurts_travelling_more_than_football(self, analyzer, profiles):
        foggy = hour(visibility_m=400, weather_code=45)
        travelling = analyzer.assess_hour(foggy, profiles["travelling"])
        football = analyzer.assess_hour(foggy, profiles["football"])
        assert travelling.score > football.score

    def test_heat_hurts_farming_more_than_a_short_jog(self, analyzer, profiles):
        hot = hour(temperature_c=34, apparent_c=35)
        farming = analyzer.assess_hour(hot, profiles["farming"])
        jogging = analyzer.assess_hour(hot, profiles["jogging"])
        # jogging's limit is lower, so its penalty saturates; farming carries
        # the higher weight. Both must be well out of the safe band.
        assert farming.band is not RiskBand.SAFE
        assert jogging.band is not RiskBand.SAFE


class TestFactorBehaviour:
    def test_uv_is_ignored_at_night(self, analyzer, profiles):
        night = analyzer.assess_hour(hour(uv=9, is_day=False), profiles["jogging"])
        assert night.factors["uv"] == 0

    def test_humidity_only_counts_when_warm(self, analyzer, profiles):
        cool = analyzer.assess_hour(hour(temperature_c=20, humidity=95), profiles["jogging"])
        warm = analyzer.assess_hour(
            hour(temperature_c=31, apparent_c=31, humidity=95), profiles["jogging"]
        )
        assert cool.factors["humidity"] == 0
        assert warm.factors["humidity"] > 50

    def test_missing_data_is_skipped_not_scored_as_zero(self, analyzer, profiles):
        blank = analyzer.assess_hour(
            hour(uv=None, visibility_m=None, humidity=None), profiles["football"]
        )
        assert "uv" not in blank.factors
        assert "visibility" not in blank.factors
        assert "humidity" not in blank.factors

    def test_intensity_counts_even_when_probability_is_low(self, analyzer, profiles):
        drizzle = analyzer.assess_hour(hour(precip_prob=5, precip_mm=0.0), profiles["picnic"])
        downpour = analyzer.assess_hour(hour(precip_prob=30, precip_mm=6.0), profiles["picnic"])
        assert downpour.factors["rain"] > drizzle.factors["rain"]


class TestBlendedScore:
    def test_one_bad_factor_is_not_averaged_away(self, analyzer, profiles):
        """
        The bug this guards against: a real Ibadan afternoon (feels like 33.5,
        rain likely) scored 18/100 -- "Safe" -- because six factors sat at zero.
        """
        hot = hour(temperature_c=29, apparent_c=33.5, precip_prob=52, humidity=70)
        assessment = analyzer.assess_hour(hot, profiles["football"])
        assert assessment.factors["heat"] > 90
        assert assessment.band is not RiskBand.SAFE

    def test_a_high_penalty_on_an_irrelevant_factor_stays_contained(
        self, analyzer, profiles
    ):
        # Football barely cares about visibility (weight 1 of a maximum 5).
        assessment = analyzer.assess_hour(hour(visibility_m=400), profiles["football"])
        assert assessment.score < 25


class TestWindowsAndDays:
    def test_worst_hour_drives_the_window(self, analyzer, profiles):
        hours = [hour(), hour(weather_code=95), hour()]
        assessment = analyzer.assess_window(hours, profiles["football"])
        assert assessment.band is RiskBand.AVOID
        assert "thunderstorm" in assessment.hard_stops

    def test_window_factors_are_averaged(self, analyzer, profiles):
        hours = [hour(precip_prob=0), hour(precip_prob=70)]
        assessment = analyzer.assess_window(hours, profiles["picnic"])
        single = analyzer.assess_hour(hour(precip_prob=70), profiles["picnic"])
        assert 0 < assessment.factors["rain"] < single.factors["rain"]

    def test_empty_window_scores_zero(self, analyzer, profiles):
        assert analyzer.assess_window([], profiles["football"]).score == 0

    def test_day_ignores_night_for_daylight_activities(self, analyzer, profiles, location):
        # Freezing at night, pleasant by day: football must not be judged on 03:00.
        payload = make_payload(
            overrides={h: {"temperature_2m": -2, "apparent_temperature": -4} for h in range(0, 6)}
        )
        forecast = Forecast.from_api(payload, location)
        football = analyzer.assess_day(forecast, date.today(), profiles["football"])
        travelling = analyzer.assess_day(forecast, date.today(), profiles["travelling"])
        assert football.score < travelling.score

    def test_a_day_with_no_data_is_not_an_error(self, analyzer, profiles, payload, location):
        forecast = Forecast.from_api(payload, location)
        assessment = analyzer.assess_day(forecast, date(2000, 1, 1), profiles["football"])
        assert assessment.score == 0
        assert assessment.reasons


class TestStormDay:
    def test_the_storm_fixture_is_avoid(self, analyzer, profiles, storm_payload, location):
        forecast = Forecast.from_api(storm_payload, location)
        assessment = analyzer.assess_day(forecast, date.today(), profiles["outdoor_event"])
        assert assessment.band is RiskBand.AVOID
