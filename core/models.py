"""
The domain model: six dataclasses plus two small enums.

These objects are deliberately dumb. They hold data and answer questions about
it; they never fetch, score or draw anything. That is what lets a test build a
Forecast by hand and drive the entire rest of the app with no network at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any

from core.exceptions import MissingDataError

# ---------------------------------------------------------------------------
# WMO weather interpretation codes used by Open-Meteo
# ---------------------------------------------------------------------------
WMO_CODES: dict[int, str] = {
    0: "Clear sky",
    1: "Mainly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Freezing fog",
    51: "Light drizzle",
    53: "Moderate drizzle",
    55: "Dense drizzle",
    56: "Light freezing drizzle",
    57: "Dense freezing drizzle",
    61: "Slight rain",
    63: "Moderate rain",
    65: "Heavy rain",
    66: "Light freezing rain",
    67: "Heavy freezing rain",
    71: "Slight snowfall",
    73: "Moderate snowfall",
    75: "Heavy snowfall",
    77: "Snow grains",
    80: "Slight rain showers",
    81: "Moderate rain showers",
    82: "Violent rain showers",
    85: "Slight snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm with slight hail",
    99: "Thunderstorm with heavy hail",
}


def describe_code(code: int | None) -> str:
    """Human wording for a WMO code, with a safe fallback."""
    if code is None:
        return "Unknown"
    return WMO_CODES.get(int(code), f"Weather code {int(code)}")


def _parse_dt(value: Any) -> datetime | None:
    """Open-Meteo returns naive local ISO strings like '2026-09-02T14:00'."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------
@dataclass
class Location:
    name: str
    latitude: float
    longitude: float
    country: str = ""
    admin1: str = ""
    timezone: str = "auto"

    @property
    def display_name(self) -> str:
        parts = [self.name]
        if self.admin1 and self.admin1 != self.name:
            parts.append(self.admin1)
        if self.country:
            parts.append(self.country)
        return ", ".join(parts)

    def key(self) -> str:
        """Stable identity for de-duplication and cache filenames."""
        return f"{round(self.latitude, 2)}_{round(self.longitude, 2)}"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "latitude": self.latitude,
            "longitude": self.longitude,
            "country": self.country,
            "admin1": self.admin1,
            "timezone": self.timezone,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Location":
        return cls(
            name=data.get("name", "Unknown"),
            latitude=float(data["latitude"]),
            longitude=float(data["longitude"]),
            country=data.get("country", "") or "",
            admin1=data.get("admin1", "") or "",
            timezone=data.get("timezone", "auto") or "auto",
        )


# ---------------------------------------------------------------------------
# One forecast hour
# ---------------------------------------------------------------------------
@dataclass
class HourPoint:
    time: datetime
    temperature_c: float | None = None
    apparent_c: float | None = None
    humidity: float | None = None
    precip_mm: float | None = None
    precip_prob: float | None = None
    weather_code: int | None = None
    wind_kmh: float | None = None
    gust_kmh: float | None = None
    uv: float | None = None
    visibility_m: float | None = None
    cloud_cover: float | None = None
    is_day: bool = True

    @property
    def description(self) -> str:
        return describe_code(self.weather_code)

    @property
    def feels_like(self) -> float | None:
        """Apparent temperature when present, otherwise plain air temperature."""
        return self.apparent_c if self.apparent_c is not None else self.temperature_c

    @property
    def is_daylight(self) -> bool:
        return bool(self.is_day)

    @property
    def label(self) -> str:
        return self.time.strftime("%H:%M")

    def to_dict(self) -> dict:
        data = {k: v for k, v in self.__dict__.items()}
        data["time"] = self.time.isoformat()
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "HourPoint":
        payload = dict(data)
        payload["time"] = _parse_dt(payload.get("time")) or datetime.min
        return cls(**payload)


# ---------------------------------------------------------------------------
# One forecast day
# ---------------------------------------------------------------------------
@dataclass
class DaySummary:
    day: date
    weather_code: int | None = None
    temp_max: float | None = None
    temp_min: float | None = None
    precip_sum: float | None = None
    precip_prob_max: float | None = None
    wind_max: float | None = None
    uv_max: float | None = None
    sunrise: datetime | None = None
    sunset: datetime | None = None

    @property
    def description(self) -> str:
        return describe_code(self.weather_code)

    def to_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "weather_code": self.weather_code,
            "temp_max": self.temp_max,
            "temp_min": self.temp_min,
            "precip_sum": self.precip_sum,
            "precip_prob_max": self.precip_prob_max,
            "wind_max": self.wind_max,
            "uv_max": self.uv_max,
            "sunrise": self.sunrise.isoformat() if self.sunrise else None,
            "sunset": self.sunset.isoformat() if self.sunset else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "DaySummary":
        return cls(
            day=date.fromisoformat(data["day"]),
            weather_code=data.get("weather_code"),
            temp_max=data.get("temp_max"),
            temp_min=data.get("temp_min"),
            precip_sum=data.get("precip_sum"),
            precip_prob_max=data.get("precip_prob_max"),
            wind_max=data.get("wind_max"),
            uv_max=data.get("uv_max"),
            sunrise=_parse_dt(data.get("sunrise")),
            sunset=_parse_dt(data.get("sunset")),
        )


# ---------------------------------------------------------------------------
# The forecast
# ---------------------------------------------------------------------------
@dataclass
class Forecast:
    location: Location
    hours: list[HourPoint] = field(default_factory=list)
    days: list[DaySummary] = field(default_factory=list)
    fetched_at: datetime = field(default_factory=datetime.now)
    from_cache: bool = False

    # -- construction -------------------------------------------------------
    @classmethod
    def from_api(
        cls,
        payload: dict,
        location: Location,
        *,
        fetched_at: datetime | None = None,
        from_cache: bool = False,
    ) -> "Forecast":
        """
        Zip the parallel arrays Open-Meteo returns into HourPoint objects.

        A `null` inside an array is a hole, not a crash -- it becomes None on
        the HourPoint and the affected factor is skipped when scoring. Only a
        missing *time* series makes the response unusable.
        """
        hourly = payload.get("hourly") or {}
        times = hourly.get("time") or []
        if not times:
            raise MissingDataError(
                "The weather service returned no hourly data for that place.",
                hint="Try again in a moment, or pick a nearby town.",
            )

        def series(name: str) -> list:
            values = hourly.get(name) or []
            # Pad short series rather than letting an index error escape.
            return list(values) + [None] * (len(times) - len(values))

        temperature = series("temperature_2m")
        apparent = series("apparent_temperature")
        humidity = series("relative_humidity_2m")
        precip_prob = series("precipitation_probability")
        precip = series("precipitation")
        codes = series("weather_code")
        wind = series("wind_speed_10m")
        gusts = series("wind_gusts_10m")
        uv = series("uv_index")
        visibility = series("visibility")
        cloud = series("cloud_cover")
        is_day = series("is_day")

        hours: list[HourPoint] = []
        for i, stamp in enumerate(times):
            parsed = _parse_dt(stamp)
            if parsed is None:
                continue
            hours.append(
                HourPoint(
                    time=parsed,
                    temperature_c=temperature[i],
                    apparent_c=apparent[i],
                    humidity=humidity[i],
                    precip_mm=precip[i],
                    precip_prob=precip_prob[i],
                    weather_code=None if codes[i] is None else int(codes[i]),
                    wind_kmh=wind[i],
                    gust_kmh=gusts[i],
                    uv=uv[i],
                    visibility_m=visibility[i],
                    cloud_cover=cloud[i],
                    is_day=True if is_day[i] is None else bool(is_day[i]),
                )
            )

        if not hours:
            raise MissingDataError(
                "The forecast came back empty.", hint="Try again in a moment."
            )

        daily = payload.get("daily") or {}
        day_list = daily.get("time") or []

        def dseries(name: str) -> list:
            values = daily.get(name) or []
            return list(values) + [None] * (len(day_list) - len(values))

        d_code = dseries("weather_code")
        d_max = dseries("temperature_2m_max")
        d_min = dseries("temperature_2m_min")
        d_psum = dseries("precipitation_sum")
        d_pprob = dseries("precipitation_probability_max")
        d_wind = dseries("wind_speed_10m_max")
        d_uv = dseries("uv_index_max")
        d_rise = dseries("sunrise")
        d_set = dseries("sunset")

        days: list[DaySummary] = []
        for i, stamp in enumerate(day_list):
            try:
                parsed_day = date.fromisoformat(str(stamp))
            except ValueError:
                continue
            days.append(
                DaySummary(
                    day=parsed_day,
                    weather_code=None if d_code[i] is None else int(d_code[i]),
                    temp_max=d_max[i],
                    temp_min=d_min[i],
                    precip_sum=d_psum[i],
                    precip_prob_max=d_pprob[i],
                    wind_max=d_wind[i],
                    uv_max=d_uv[i],
                    sunrise=_parse_dt(d_rise[i]),
                    sunset=_parse_dt(d_set[i]),
                )
            )

        resolved = Location(
            name=location.name,
            latitude=location.latitude,
            longitude=location.longitude,
            country=location.country,
            admin1=location.admin1,
            timezone=payload.get("timezone") or location.timezone,
        )
        return cls(
            location=resolved,
            hours=hours,
            days=days,
            fetched_at=fetched_at or datetime.now(),
            from_cache=from_cache,
        )

    # -- queries ------------------------------------------------------------
    def hours_for_date(self, day: date) -> list[HourPoint]:
        return [h for h in self.hours if h.time.date() == day]

    def hours_between(self, start: datetime, end: datetime) -> list[HourPoint]:
        return [h for h in self.hours if start <= h.time <= end]

    def day(self, day: date) -> DaySummary | None:
        for summary in self.days:
            if summary.day == day:
                return summary
        return None

    def available_dates(self) -> list[date]:
        seen: list[date] = []
        for hour in self.hours:
            if hour.time.date() not in seen:
                seen.append(hour.time.date())
        return seen

    def is_stale(self, minutes: int = 30) -> bool:
        return datetime.now() - self.fetched_at > timedelta(minutes=minutes)

    @property
    def age_minutes(self) -> int:
        return int((datetime.now() - self.fetched_at).total_seconds() // 60)

    # -- round trip ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "location": self.location.to_dict(),
            "hours": [h.to_dict() for h in self.hours],
            "days": [d.to_dict() for d in self.days],
            "fetched_at": self.fetched_at.isoformat(),
            "from_cache": self.from_cache,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Forecast":
        return cls(
            location=Location.from_dict(data["location"]),
            hours=[HourPoint.from_dict(h) for h in data.get("hours", [])],
            days=[DaySummary.from_dict(d) for d in data.get("days", [])],
            fetched_at=_parse_dt(data.get("fetched_at")) or datetime.now(),
            from_cache=bool(data.get("from_cache")),
        )


# ---------------------------------------------------------------------------
# Risk
# ---------------------------------------------------------------------------
class RiskBand(Enum):
    """The four words from the brief -- the app's spine."""

    SAFE = "Safe"
    MANAGEABLE = "Manageable"
    RISKY = "Risky"
    AVOID = "Avoid"

    @classmethod
    def from_score(cls, score: float) -> "RiskBand":
        if score < 25:
            return cls.SAFE
        if score < 50:
            return cls.MANAGEABLE
        if score < 75:
            return cls.RISKY
        return cls.AVOID

    @property
    def colour(self) -> str:
        return {
            RiskBand.SAFE: "#1b7a4e",
            RiskBand.MANAGEABLE: "#8a6810",
            RiskBand.RISKY: "#b0521e",
            RiskBand.AVOID: "#9c1f28",
        }[self]

    @property
    def verdict_sentence(self) -> str:
        return {
            RiskBand.SAFE: "Conditions are good for this activity.",
            RiskBand.MANAGEABLE: "Workable, but prepare for the conditions.",
            RiskBand.RISKY: "Only with real precautions, or pick another time.",
            RiskBand.AVOID: "Do not do this in these conditions.",
        }[self]


FACTOR_LABELS = {
    "heat": "Heat",
    "cold": "Cold",
    "rain": "Rain",
    "wind": "Wind",
    "uv": "UV",
    "storm": "Storm",
    "visibility": "Visibility",
    "humidity": "Humidity",
}


@dataclass
class RiskAssessment:
    score: float
    factors: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    hard_stops: list[str] = field(default_factory=list)

    @property
    def band(self) -> RiskBand:
        return RiskBand.from_score(self.score)

    def top_factors(self, count: int = 4) -> list[tuple[str, float]]:
        return sorted(self.factors.items(), key=lambda kv: kv[1], reverse=True)[:count]

    def to_dict(self) -> dict:
        return {
            "score": round(self.score, 1),
            "band": self.band.value,
            "factors": {k: round(v, 1) for k, v in self.factors.items()},
            "reasons": list(self.reasons),
            "hard_stops": list(self.hard_stops),
        }


@dataclass
class TimeWindow:
    start: datetime
    end: datetime
    assessment: RiskAssessment

    @property
    def label(self) -> str:
        return f"{self.start:%H:%M}–{self.end:%H:%M}"

    def to_dict(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "label": self.label,
            "assessment": self.assessment.to_dict(),
        }


# ---------------------------------------------------------------------------
# Activity profile -- loaded from data/activities.json, never hard-coded
# ---------------------------------------------------------------------------
@dataclass
class ActivityProfile:
    key: str
    label: str
    duration_hours: int
    daylight_only: bool
    weights: dict[str, float]
    ideal: dict[str, Any]
    limit: dict[str, Any]
    packing: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, key: str, data: dict) -> "ActivityProfile":
        return cls(
            key=key,
            label=data.get("label", key.replace("_", " ").title()),
            duration_hours=int(data.get("duration_hours", 2)),
            daylight_only=bool(data.get("daylight_only", False)),
            weights=dict(data.get("weights", {})),
            ideal=dict(data.get("ideal", {})),
            limit=dict(data.get("limit", {})),
            packing=list(data.get("packing", [])),
        )

    # Convenience accessors so the analyzer stays readable
    @property
    def ideal_temp(self) -> tuple[float, float]:
        low, high = self.ideal.get("temp_c", [15, 28])
        return float(low), float(high)

    @property
    def limit_temp(self) -> tuple[float, float]:
        low, high = self.limit.get("temp_c", [5, 35])
        return float(low), float(high)


@dataclass
class ChecklistItem:
    item: str
    reason: str = ""
    essential: bool = False
    checked: bool = False

    def to_dict(self) -> dict:
        return {
            "item": self.item,
            "reason": self.reason,
            "essential": self.essential,
            "checked": self.checked,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ChecklistItem":
        return cls(
            item=str(data.get("item", "")).strip(),
            reason=str(data.get("reason", "") or "").strip(),
            essential=bool(data.get("essential")),
            checked=bool(data.get("checked")),
        )


@dataclass
class AIExplanation:
    verdict: str
    headline: str
    explanation: str
    safety_advice: list[str] = field(default_factory=list)
    packing: list[ChecklistItem] = field(default_factory=list)
    timing_note: str = ""


# ---------------------------------------------------------------------------
# The finished plan
# ---------------------------------------------------------------------------
@dataclass
class ActivityPlan:
    location: Location
    activity_key: str
    activity_label: str
    day: date
    assessment: RiskAssessment
    windows: list[TimeWindow] = field(default_factory=list)
    headline: str = ""
    explanation: str = ""
    advice: list[str] = field(default_factory=list)
    checklist: list[ChecklistItem] = field(default_factory=list)
    timing_note: str = ""
    ai_used: bool = False
    ai_note: str = ""
    forecast_from_cache: bool = False
    forecast_fetched_at: datetime | None = None
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def band(self) -> RiskBand:
        return self.assessment.band

    @property
    def best_window(self) -> TimeWindow | None:
        return self.windows[0] if self.windows else None

    def to_dict(self) -> dict:
        return {
            "location": self.location.to_dict(),
            "activity_key": self.activity_key,
            "activity_label": self.activity_label,
            "day": self.day.isoformat(),
            "assessment": self.assessment.to_dict(),
            "windows": [w.to_dict() for w in self.windows],
            "headline": self.headline,
            "explanation": self.explanation,
            "advice": list(self.advice),
            "checklist": [c.to_dict() for c in self.checklist],
            "timing_note": self.timing_note,
            "ai_used": self.ai_used,
            "ai_note": self.ai_note,
            "forecast_from_cache": self.forecast_from_cache,
            "forecast_fetched_at": (
                self.forecast_fetched_at.isoformat() if self.forecast_fetched_at else None
            ),
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ActivityPlan":
        assessment_data = data.get("assessment", {})
        assessment = RiskAssessment(
            score=float(assessment_data.get("score", 0)),
            factors=dict(assessment_data.get("factors", {})),
            reasons=list(assessment_data.get("reasons", [])),
            hard_stops=list(assessment_data.get("hard_stops", [])),
        )
        windows = []
        for w in data.get("windows", []):
            w_assess = w.get("assessment", {})
            windows.append(
                TimeWindow(
                    start=_parse_dt(w["start"]) or datetime.min,
                    end=_parse_dt(w["end"]) or datetime.min,
                    assessment=RiskAssessment(
                        score=float(w_assess.get("score", 0)),
                        factors=dict(w_assess.get("factors", {})),
                        reasons=list(w_assess.get("reasons", [])),
                        hard_stops=list(w_assess.get("hard_stops", [])),
                    ),
                )
            )
        return cls(
            location=Location.from_dict(data["location"]),
            activity_key=data.get("activity_key", ""),
            activity_label=data.get("activity_label", ""),
            day=date.fromisoformat(data["day"]),
            assessment=assessment,
            windows=windows,
            headline=data.get("headline", ""),
            explanation=data.get("explanation", ""),
            advice=list(data.get("advice", [])),
            checklist=[ChecklistItem.from_dict(c) for c in data.get("checklist", [])],
            timing_note=data.get("timing_note", ""),
            ai_used=bool(data.get("ai_used")),
            ai_note=data.get("ai_note", ""),
            forecast_from_cache=bool(data.get("forecast_from_cache")),
            forecast_fetched_at=_parse_dt(data.get("forecast_fetched_at")),
            created_at=_parse_dt(data.get("created_at")) or datetime.now(),
        )

    def to_text(self) -> str:
        """A printable plain-text version, saved alongside the JSON."""
        lines = [
            f"{self.activity_label.upper()} PLAN",
            f"{self.location.display_name} — {self.day:%A %d %B %Y}",
            "=" * 62,
            "",
            f"VERDICT: {self.band.value.upper()}  ({self.assessment.score:.0f}/100)",
            self.headline or self.band.verdict_sentence,
            "",
        ]

        if self.explanation:
            lines += ["WHY", self.explanation, ""]

        if self.assessment.reasons:
            lines += ["SAFETY FLAGS"]
            lines += [f"  ! {r}" for r in self.assessment.reasons]
            lines.append("")

        if self.windows:
            lines.append("BEST TIMES")
            for i, window in enumerate(self.windows, 1):
                marker = "BEST" if i == 1 else f"  #{i}"
                lines.append(
                    f"  {marker}  {window.label}   "
                    f"{window.assessment.band.value} ({window.assessment.score:.0f}/100)"
                )
            if self.timing_note:
                lines.append(f"  {self.timing_note}")
            lines.append("")

        if self.assessment.factors:
            lines.append("CONDITION BREAKDOWN")
            for name, value in self.assessment.top_factors(8):
                bar = "#" * int(round(value / 10)) + "." * (10 - int(round(value / 10)))
                lines.append(f"  {FACTOR_LABELS.get(name, name):<11} {bar} {value:>3.0f}")
            lines.append("")

        if self.advice:
            lines.append("SAFETY ADVICE")
            lines += [f"  - {a}" for a in self.advice]
            lines.append("")

        if self.checklist:
            lines.append("PACKING CHECKLIST")
            for item in self.checklist:
                mark = "[x]" if item.checked else "[ ]"
                star = "*" if item.essential else " "
                reason = f"  ({item.reason})" if item.reason else ""
                lines.append(f"  {mark}{star} {item.item}{reason}")
            lines.append("")

        source = "cached forecast" if self.forecast_from_cache else "live forecast"
        engine = "Gemini" if self.ai_used else "built-in rules"
        lines += [
            "-" * 62,
            f"Generated {self.created_at:%Y-%m-%d %H:%M} from a {source}; wording by {engine}.",
        ]
        if self.ai_note:
            lines.append(self.ai_note)
        return "\n".join(lines)
