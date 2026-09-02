"""
RecommendationEngine -- turns scores into the four things the brief asks for:
a verdict people can act on, the best time of day, safety advice, and a packing
checklist.

The engine holds an analyzer and an *optional* AI client. Every method returns
a complete result with the AI client absent, failing, or disabled: Gemini
improves the wording, it is never load-bearing.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from core.exceptions import AIError, NoViableWindowError, UnknownActivityError
from core.models import (
    ActivityPlan,
    ActivityProfile,
    ChecklistItem,
    Forecast,
    RiskAssessment,
    TimeWindow,
)
from core.risk_analyzer import ActivityRiskAnalyzer

log = logging.getLogger(__name__)


class RecommendationEngine:
    def __init__(
        self,
        analyzer: ActivityRiskAnalyzer,
        profiles: dict[str, ActivityProfile],
        ai_client=None,
    ):
        self.analyzer = analyzer
        self.profiles = profiles
        self.ai_client = ai_client

    # -----------------------------------------------------------------------
    def profile_for(self, activity_key: str) -> ActivityProfile:
        try:
            return self.profiles[activity_key]
        except KeyError as exc:
            raise UnknownActivityError(
                f"{activity_key!r} is not a known activity.",
                hint=f"Known activities: {', '.join(sorted(self.profiles))}",
                cause=exc,
            ) from exc

    # -----------------------------------------------------------------------
    # Best time of day
    # -----------------------------------------------------------------------
    def best_windows(
        self, forecast: Forecast, day: date, activity_key: str, top_n: int = 3
    ) -> list[TimeWindow]:
        """
        Slide a duration-length window across the day and rank by risk.

        Three windows are returned rather than one, so the user can trade
        "best" against "possible for me" and see why one beats another.
        """
        profile = self.profile_for(activity_key)
        hours = forecast.hours_for_date(day)
        span = max(1, int(profile.duration_hours))

        if len(hours) < span:
            raise NoViableWindowError(
                profile.label,
                day,
                hint=f"Only {len(hours)} forecast hours are available for that day.",
            )

        windows: list[TimeWindow] = []
        for i in range(len(hours) - span + 1):
            chunk = hours[i : i + span]
            if profile.daylight_only and not all(h.is_daylight for h in chunk):
                continue
            windows.append(
                TimeWindow(
                    start=chunk[0].time,
                    end=chunk[-1].time + timedelta(hours=1),
                    assessment=self.analyzer.assess_window(chunk, profile),
                )
            )

        if not windows:
            raise NoViableWindowError(
                profile.label,
                day,
                hint=f"{profile.label} needs {span} daylight hours in a row.",
            )

        windows.sort(key=lambda w: (w.assessment.score, w.start))
        return windows[:top_n]

    # -----------------------------------------------------------------------
    # The whole plan
    # -----------------------------------------------------------------------
    def build_plan(self, forecast: Forecast, day: date, activity_key: str) -> ActivityPlan:
        """Assemble everything, then let the AI narrate it if it can."""
        profile = self.profile_for(activity_key)
        assessment = self.analyzer.assess_day(forecast, day, profile)

        windows: list[TimeWindow] = []
        timing_note = ""
        try:
            windows = self.best_windows(forecast, day, activity_key)
        except NoViableWindowError as exc:
            # Not fatal: the day verdict still stands, there is just no slot.
            log.info("No viable window: %s", exc)
            timing_note = f"{exc.user_message} {exc.hint}".strip()

        plan = ActivityPlan(
            location=forecast.location,
            activity_key=profile.key,
            activity_label=profile.label,
            day=day,
            assessment=assessment,
            windows=windows,
            headline=self._headline(assessment, profile, windows),
            explanation=self._rule_based_explanation(assessment, profile, windows),
            advice=self._rule_based_advice(assessment, profile),
            checklist=self._packing_list(assessment, profile, forecast, day),
            timing_note=timing_note or self._timing_note(windows),
            forecast_from_cache=forecast.from_cache,
            forecast_fetched_at=forecast.fetched_at,
        )

        self._apply_ai(plan, forecast, profile)
        return plan

    # -----------------------------------------------------------------------
    # AI narration (optional, never load-bearing)
    # -----------------------------------------------------------------------
    def _apply_ai(self, plan: ActivityPlan, forecast: Forecast, profile: ActivityProfile) -> None:
        if not self.ai_client or not getattr(self.ai_client, "available", False):
            plan.ai_note = "AI explanation unavailable — showing standard guidance."
            return

        try:
            explanation = self.ai_client.explain(self._ai_context(plan, forecast))
        except AIError as exc:
            log.warning("AI narration failed: %s", exc)
            plan.ai_note = exc.user_message
            return
        except Exception as exc:  # defensive: a broken AI must never break the plan
            log.exception("Unexpected AI failure")
            plan.ai_note = "AI explanation unavailable — showing standard guidance."
            return

        plan.ai_used = True
        plan.headline = explanation.headline or plan.headline
        plan.explanation = explanation.explanation or plan.explanation
        if explanation.safety_advice:
            plan.advice = explanation.safety_advice
        if explanation.timing_note:
            plan.timing_note = explanation.timing_note
        if explanation.packing:
            plan.checklist = self._merge_packing(plan.checklist, explanation.packing)

    def _ai_context(self, plan: ActivityPlan, forecast: Forecast) -> dict:
        hours = forecast.hours_for_date(plan.day)
        lines = []
        for hour in hours:
            if not hour.is_daylight and len(hours) > 12:
                continue  # keep the prompt tight; night hours rarely matter
            feels = f"{hour.feels_like:.1f}" if hour.feels_like is not None else "?"
            temp = f"{hour.temperature_c:.1f}" if hour.temperature_c is not None else "?"
            lines.append(
                f"  {hour.label}  {temp}°C feels {feels}  "
                f"UV {hour.uv if hour.uv is not None else '?'}  "
                f"rain {hour.precip_prob if hour.precip_prob is not None else '?'}%  "
                f"wind {hour.wind_kmh if hour.wind_kmh is not None else '?'} km/h  "
                f"{hour.description}"
            )
        return {
            "location": plan.location.display_name,
            "activity": plan.activity_label,
            "day": plan.day.isoformat(),
            "band": plan.band.value,
            "score": plan.assessment.score,
            "factors": plan.assessment.factors,
            "reasons": plan.assessment.reasons,
            "windows": [
                f"{w.label} — {w.assessment.band.value} ({w.assessment.score:.0f}/100)"
                for w in plan.windows
            ],
            "hourly_lines": lines,
        }

    @staticmethod
    def _merge_packing(
        rule_based: list[ChecklistItem], ai_items: list[ChecklistItem]
    ) -> list[ChecklistItem]:
        """Keep the rule-based essentials, add whatever the AI thought of."""
        merged: list[ChecklistItem] = []
        seen: set[str] = set()
        for item in list(rule_based) + list(ai_items):
            name = item.item.strip().lower()
            if not name or name in seen:
                continue
            seen.add(name)
            merged.append(item)
        return merged[:14]

    # -----------------------------------------------------------------------
    # Rule-based wording -- the fallback, and the baseline the AI improves on
    # -----------------------------------------------------------------------
    @staticmethod
    def _headline(
        assessment: RiskAssessment, profile: ActivityProfile, windows: list[TimeWindow]
    ) -> str:
        band = assessment.band
        if windows and band.value in ("Safe", "Manageable"):
            return f"{profile.label} is fine here — aim for {windows[0].label}."
        if windows and band.value == "Risky":
            return f"{profile.label} is risky today; {windows[0].label} is the least bad slot."
        if band.value == "Avoid":
            return f"Call off {profile.label.lower()} for this day."
        return f"{profile.label}: {band.verdict_sentence}"

    @staticmethod
    def _timing_note(windows: list[TimeWindow]) -> str:
        if not windows:
            return ""
        best = windows[0]
        if len(windows) == 1:
            return f"Only one workable slot was found: {best.label}."
        spread = windows[-1].assessment.score - best.assessment.score
        if spread < 5:
            return "The day is fairly even — any of these slots works about as well."
        return f"{best.label} is clearly the best of the day."

    def _rule_based_explanation(
        self, assessment: RiskAssessment, profile: ActivityProfile, windows: list[TimeWindow]
    ) -> str:
        parts = [
            f"Scored {assessment.score:.0f} out of 100 for {profile.label.lower()}, "
            f"which puts the day in the {assessment.band.value.lower()} band."
        ]
        drivers = [(n, v) for n, v in assessment.top_factors(3) if v >= 20]
        if drivers:
            named = ", ".join(f"{n} {v:.0f}" for n, v in drivers)
            parts.append(f"The conditions pushing the score up are {named}.")
        else:
            parts.append("No single condition stands out as a problem.")
        if windows:
            parts.append(
                f"The lowest-risk {profile.duration_hours}-hour slot is "
                f"{windows[0].label}, scoring {windows[0].assessment.score:.0f}."
            )
        if assessment.reasons:
            parts.append(assessment.reasons[0])
        return " ".join(parts)

    def _rule_based_advice(
        self, assessment: RiskAssessment, profile: ActivityProfile
    ) -> list[str]:
        """Advice keyed off the factor sub-scores, worst first."""
        f = assessment.factors
        advice: list[str] = []

        if "thunderstorm" in assessment.hard_stops:
            advice.append(
                "Do not be in the open during the storm — lightning is the danger, not the rain."
            )
        if "gusts" in assessment.hard_stops:
            advice.append("Take down canopies, gazebos and anything else the wind can lift.")

        if f.get("heat", 0) >= 60:
            advice.append(
                "Shift to early morning or evening; the middle of the day is too hot for "
                f"{profile.duration_hours} hours of activity."
            )
        elif f.get("heat", 0) >= 30:
            advice.append("Take a shaded break every 30 minutes and drink before you feel thirsty.")

        if f.get("cold", 0) >= 40:
            advice.append("Dress in layers and warm up properly before starting.")

        if f.get("rain", 0) >= 60:
            advice.append("Expect to get wet — bring a change of clothes and protect anything electronic.")
        elif f.get("rain", 0) >= 30:
            advice.append("Showers are likely; pack a waterproof and plan where you would shelter.")

        if f.get("wind", 0) >= 50:
            advice.append("Secure loose equipment; the wind is strong enough to move it.")

        if f.get("uv", 0) >= 50:
            advice.append("Apply SPF 50 before you go out and reapply every two hours.")
        elif f.get("uv", 0) >= 25:
            advice.append("Wear a hat and sunglasses — the UV index is above comfortable levels.")

        if f.get("humidity", 0) >= 50:
            advice.append("Humidity will slow how fast you cool down; ease the pace and rest often.")

        if f.get("visibility", 0) >= 40:
            advice.append("Visibility is poor — wear something bright and allow extra travel time.")

        if f.get("storm", 0) >= 30 and "thunderstorm" not in assessment.hard_stops:
            advice.append("Watch the sky and have an indoor fallback ready.")

        if not advice:
            advice.append("Conditions are within normal limits — the usual precautions are enough.")
        return advice[:6]

    def _packing_list(
        self,
        assessment: RiskAssessment,
        profile: ActivityProfile,
        forecast: Forecast,
        day: date,
    ) -> list[ChecklistItem]:
        """The profile's own kit, plus items earned by today's conditions."""
        items: list[ChecklistItem] = [
            ChecklistItem(item=name, reason=f"Standard for {profile.label.lower()}", essential=True)
            for name in profile.packing
        ]
        f = assessment.factors

        def add(name: str, reason: str, essential: bool = False) -> None:
            if not any(i.item.lower() == name.lower() for i in items):
                items.append(ChecklistItem(item=name, reason=reason, essential=essential))

        if f.get("rain", 0) >= 25:
            add("Waterproof jacket", f"Rain risk scored {f['rain']:.0f}/100", True)
        if f.get("rain", 0) >= 55:
            add("Dry bag for phone and keys", "Heavy rain likely")
            add("Change of clothes", "You are likely to get soaked")
        if f.get("heat", 0) >= 35:
            add("Extra 1L of water", f"Heat scored {f['heat']:.0f}/100", True)
            add("Electrolyte sachets", "Sustained heat means salt loss as well as water")
        if f.get("uv", 0) >= 30:
            add("SPF 50 sunscreen", f"UV scored {f['uv']:.0f}/100", True)
            add("Sunglasses", "Strong sun forecast")
        if f.get("cold", 0) >= 35:
            add("Warm layer", f"Cold scored {f['cold']:.0f}/100", True)
        if f.get("wind", 0) >= 40:
            add("Windproof top", f"Wind scored {f['wind']:.0f}/100")
        if f.get("humidity", 0) >= 45:
            add("Sweat towel", "High humidity slows cooling")
        if f.get("visibility", 0) >= 35:
            add("High-visibility clothing", "Poor visibility forecast", True)
            add("Torch or head torch", "Poor visibility forecast")
        if f.get("storm", 0) >= 30:
            add("Indoor fallback plan", "Hazardous weather is possible", True)

        hours = forecast.hours_for_date(day)
        if hours and any(not h.is_daylight for h in hours[-3:]) and not profile.daylight_only:
            add("Head torch", "The activity may run past sunset")

        return items[:14]
