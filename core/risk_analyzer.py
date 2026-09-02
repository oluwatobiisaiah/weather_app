"""
ActivityRiskAnalyzer -- where the verdict is actually decided.

Pure functions over data: no network, no files, no widgets. That is deliberate.
Because this module has no I/O it can be tested exhaustively, and because it is
tested exhaustively the verdict it produces is the one thing in the app allowed
to be authoritative. Gemini explains what this file decides; it never overrules
it.

Scoring model
-------------
Each of eight factors returns a penalty from 0 (perfect) to 100 (dangerous),
using a piecewise-linear curve: zero inside the profile's `ideal` range, ramping
to 100 at the `limit`, pinned at 100 beyond.

Those penalties are then combined by BLEND_MEAN/BLEND_WORST, not by a plain
weighted mean. A plain mean was the first design and it was wrong: on a real
Ibadan afternoon -- feels like 33.5 °C, 74% chance of rain -- six factors sitting
at zero dragged a heat penalty of 100 down to an overall 18, and the app called
a dangerous afternoon "Safe". Averages dilute. So the score is a blend of the
weighted mean (what the day is like overall) and the worst single factor scaled
by how much this activity cares about it (what will actually hurt you).

Hard stops then sit on top of the arithmetic, for the same reason at a larger
scale: one thunderstorm hour inside a pleasant afternoon must not average away.
"""

from __future__ import annotations

import logging

from core.models import (
    ActivityProfile,
    Forecast,
    HourPoint,
    RiskAssessment,
    describe_code,
)

log = logging.getLogger(__name__)

FACTORS = ("heat", "cold", "rain", "wind", "uv", "storm", "visibility", "humidity")

#: Hazardous *phenomena*, as opposed to plain wetness which the rain factor owns.
STORM_PENALTY: dict[int, float] = {
    45: 40,   # fog
    48: 55,   # freezing fog
    56: 60,   # light freezing drizzle
    57: 70,   # dense freezing drizzle
    66: 75,   # light freezing rain
    67: 85,   # heavy freezing rain
    71: 45,   # slight snow
    73: 60,   # moderate snow
    75: 75,   # heavy snow
    77: 40,   # snow grains
    82: 85,   # violent rain showers
    85: 50,   # slight snow showers
    86: 70,   # heavy snow showers
    95: 100,  # thunderstorm
    96: 100,  # thunderstorm with hail
    99: 100,  # thunderstorm with heavy hail
}

#: Codes that end the discussion regardless of everything else.
THUNDERSTORM_CODES = frozenset({95, 96, 99})

#: Score floors applied by hard stops.
FLOOR_STORM = 80.0
FLOOR_GUST = 75.0
FLOOR_EXTREME_TEMP = 75.0

#: How the overall score splits between "the day overall" and "the worst thing
#: about it". These two must sum to 1.0.
BLEND_MEAN = 0.45
BLEND_WORST = 0.55


# ---------------------------------------------------------------------------
# Ramps
# ---------------------------------------------------------------------------
def ramp_up(value: float, ideal_edge: float, limit_edge: float) -> float:
    """0 at the ideal edge, 100 at the limit edge, clamped both ways."""
    if limit_edge <= ideal_edge:
        return 0.0 if value <= ideal_edge else 100.0
    if value <= ideal_edge:
        return 0.0
    if value >= limit_edge:
        return 100.0
    return (value - ideal_edge) / (limit_edge - ideal_edge) * 100.0


def ramp_down(value: float, ideal_edge: float, limit_edge: float) -> float:
    """The same curve for factors where *lower* is worse (cold, visibility)."""
    if ideal_edge <= limit_edge:
        return 0.0 if value >= ideal_edge else 100.0
    if value >= ideal_edge:
        return 0.0
    if value <= limit_edge:
        return 100.0
    return (ideal_edge - value) / (ideal_edge - limit_edge) * 100.0


class ActivityRiskAnalyzer:
    """Scores hours, windows and days against an activity profile."""

    def __init__(self, profiles: dict[str, ActivityProfile]):
        self.profiles = profiles

    # -- one hour -----------------------------------------------------------
    def assess_hour(self, hour: HourPoint, profile: ActivityProfile) -> RiskAssessment:
        """Score a single hour. Factors with no data are skipped, not zeroed."""
        penalties: dict[str, float] = {}

        feels = hour.feels_like
        ideal_low, ideal_high = profile.ideal_temp
        limit_low, limit_high = profile.limit_temp

        if feels is not None:
            penalties["heat"] = ramp_up(feels, ideal_high, limit_high)
            penalties["cold"] = ramp_down(feels, ideal_low, limit_low)

        rain = self._rain(hour, profile)
        if rain is not None:
            penalties["rain"] = rain

        wind = self._wind(hour, profile)
        if wind is not None:
            penalties["wind"] = wind

        if hour.uv is not None:
            penalties["uv"] = (
                0.0
                if not hour.is_day
                else ramp_up(
                    float(hour.uv),
                    float(profile.ideal.get("uv", 6)),
                    float(profile.limit.get("uv", 10)),
                )
            )

        if hour.weather_code is not None:
            penalties["storm"] = STORM_PENALTY.get(int(hour.weather_code), 0.0)

        if hour.visibility_m is not None:
            penalties["visibility"] = ramp_down(float(hour.visibility_m), 5000.0, 500.0)

        humidity = self._humidity(hour)
        if humidity is not None:
            penalties["humidity"] = humidity

        score = self._combine(penalties, profile)
        reasons, hard_stops, floor = self._hard_stops(hour, profile)
        score = max(score, floor)

        return RiskAssessment(
            score=round(min(100.0, max(0.0, score)), 1),
            factors={k: round(v, 1) for k, v in penalties.items()},
            reasons=reasons,
            hard_stops=hard_stops,
        )

    # -- a span of hours ----------------------------------------------------
    def assess_window(
        self, hours: list[HourPoint], profile: ActivityProfile
    ) -> RiskAssessment:
        """
        Score a span. The mean drives the number; the worst hour drives the
        hard stops -- one thunderstorm hour ruins a two-hour match.
        """
        if not hours:
            return RiskAssessment(score=0.0)

        per_hour = [self.assess_hour(h, profile) for h in hours]

        totals: dict[str, list[float]] = {}
        for assessment in per_hour:
            for name, value in assessment.factors.items():
                totals.setdefault(name, []).append(value)
        factors = {name: sum(vals) / len(vals) for name, vals in totals.items()}

        mean_score = sum(a.score for a in per_hour) / len(per_hour)

        reasons: list[str] = []
        hard_stops: list[str] = []
        for assessment in per_hour:
            for reason in assessment.reasons:
                if reason not in reasons:
                    reasons.append(reason)
            for stop in assessment.hard_stops:
                if stop not in hard_stops:
                    hard_stops.append(stop)

        # Any hard stop inside the window applies to the whole window.
        floor = max((a.score for a in per_hour if a.hard_stops), default=0.0)
        score = max(mean_score, floor)

        return RiskAssessment(
            score=round(min(100.0, max(0.0, score)), 1),
            factors={k: round(v, 1) for k, v in factors.items()},
            reasons=reasons,
            hard_stops=hard_stops,
        )

    # -- a whole day --------------------------------------------------------
    def assess_day(self, forecast: Forecast, day, profile: ActivityProfile) -> RiskAssessment:
        """
        The day-level verdict shown at the top of the results.

        For daylight-only activities the night hours are excluded, otherwise a
        cold, dark 03:00 would drag down a perfectly good afternoon.
        """
        hours = forecast.hours_for_date(day)
        if not hours:
            return RiskAssessment(score=0.0, reasons=["No forecast data for this day."])
        if profile.daylight_only:
            daylight = [h for h in hours if h.is_daylight]
            hours = daylight or hours
        return self.assess_window(hours, profile)

    # -- internals ----------------------------------------------------------
    def _combine(self, penalties: dict[str, float], profile: ActivityProfile) -> float:
        """
        Blend the weighted mean with the worst factor this activity cares about.

        The mean alone under-reports (see the module docstring); the worst factor
        alone over-reports, turning every breezy day into a crisis. Together they
        say "the day is mostly fine, but this one thing will get you".
        """
        if not penalties:
            return 0.0

        max_weight = max((float(w) for w in profile.weights.values()), default=1.0) or 1.0

        weighted = 0.0
        weight_total = 0.0
        worst = 0.0
        for name, value in penalties.items():
            weight = float(profile.weights.get(name, 1))
            weighted += weight * value
            weight_total += weight
            # Scaling by relative weight stops a 100 on a factor this activity
            # barely cares about (fog, for football) from dominating.
            worst = max(worst, value * (weight / max_weight))

        mean = weighted / weight_total if weight_total else 0.0
        return BLEND_MEAN * mean + BLEND_WORST * worst

    def _rain(self, hour: HourPoint, profile: ActivityProfile) -> float | None:
        """Probability and intensity together: 30% chance of a downpour counts."""
        parts: list[float] = []
        if hour.precip_prob is not None:
            parts.append(
                ramp_up(
                    float(hour.precip_prob),
                    float(profile.ideal.get("precip_prob", 20)),
                    float(profile.limit.get("precip_prob", 70)),
                )
            )
        if hour.precip_mm is not None:
            parts.append(ramp_up(float(hour.precip_mm), 0.5, 7.0))
        return max(parts) if parts else None

    def _wind(self, hour: HourPoint, profile: ActivityProfile) -> float | None:
        ideal = float(profile.ideal.get("wind_kmh", 20))
        limit = float(profile.limit.get("wind_kmh", 40))
        parts: list[float] = []
        if hour.wind_kmh is not None:
            parts.append(ramp_up(float(hour.wind_kmh), ideal, limit))
        if hour.gust_kmh is not None:
            parts.append(ramp_up(float(hour.gust_kmh), ideal + 10, limit + 15))
        return max(parts) if parts else None

    def _humidity(self, hour: HourPoint) -> float | None:
        """Only matters when it is already warm -- it is a heat multiplier."""
        if hour.humidity is None:
            return None
        temperature = hour.temperature_c if hour.temperature_c is not None else hour.feels_like
        if temperature is None or temperature <= 28:
            return 0.0
        return ramp_up(float(hour.humidity), 60.0, 95.0)

    def _hard_stops(
        self, hour: HourPoint, profile: ActivityProfile
    ) -> tuple[list[str], list[str], float]:
        """Conditions that set a floor on the score whatever the mean says."""
        reasons: list[str] = []
        stops: list[str] = []
        floor = 0.0

        if hour.weather_code is not None and int(hour.weather_code) in THUNDERSTORM_CODES:
            reasons.append(
                f"{describe_code(hour.weather_code)} forecast at {hour.label} — "
                "lightning risk, stay indoors."
            )
            stops.append("thunderstorm")
            floor = max(floor, FLOOR_STORM)

        gust_limit = float(profile.limit.get("wind_kmh", 40)) + 20
        if hour.gust_kmh is not None and float(hour.gust_kmh) > gust_limit:
            reasons.append(
                f"Gusts of {float(hour.gust_kmh):.0f} km/h at {hour.label} — "
                "unsafe for canopies, equipment and light objects."
            )
            stops.append("gusts")
            floor = max(floor, FLOOR_GUST)

        feels = hour.feels_like
        if feels is not None and float(feels) > 40:
            reasons.append(
                f"Feels like {float(feels):.0f} °C at {hour.label} — serious heat-stress risk."
            )
            stops.append("extreme_heat")
            floor = max(floor, FLOOR_EXTREME_TEMP)
        elif feels is not None and float(feels) < -5:
            reasons.append(
                f"Feels like {float(feels):.0f} °C at {hour.label} — serious cold-exposure risk."
            )
            stops.append("extreme_cold")
            floor = max(floor, FLOOR_EXTREME_TEMP)

        return reasons, stops, floor
