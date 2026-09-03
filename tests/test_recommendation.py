"""Best-time search, rule-based wording, and the AI fallback path."""

from __future__ import annotations

from datetime import date

import pytest
from conftest import make_payload

from core.exceptions import AIUnavailableError, NoViableWindowError, UnknownActivityError
from core.models import AIExplanation, ChecklistItem, Forecast, Location, RiskBand


@pytest.fixture
def location() -> Location:
    return Location(name="Ibadan", latitude=7.3776, longitude=3.9059)


@pytest.fixture
def forecast(payload, location) -> Forecast:
    return Forecast.from_api(payload, location)


class TestBestWindows:
    def test_returns_three_ranked_windows(self, engine, forecast):
        windows = engine.best_windows(forecast, date.today(), "football")
        assert len(windows) == 3
        scores = [w.assessment.score for w in windows]
        assert scores == sorted(scores)

    def test_daylight_only_activities_stay_in_daylight(self, engine, forecast, location):
        # Make the morning unpleasant so the search would prefer the night.
        payload = make_payload(
            overrides={h: {"apparent_temperature": 34} for h in range(6, 19)}
        )
        forecast = Forecast.from_api(payload, location)
        for window in engine.best_windows(forecast, date.today(), "football"):
            assert 6 <= window.start.hour <= 18

    def test_night_is_allowed_for_activities_that_permit_it(self, engine, location):
        payload = make_payload(
            overrides={h: {"apparent_temperature": 34} for h in range(6, 19)}
        )
        forecast = Forecast.from_api(payload, location)
        windows = engine.best_windows(forecast, date.today(), "jogging")
        assert any(w.start.hour < 6 or w.start.hour > 18 for w in windows)

    def test_window_length_matches_the_profile(self, engine, forecast, profiles):
        window = engine.best_windows(forecast, date.today(), "farming")[0]
        span = (window.end - window.start).total_seconds() / 3600
        assert span == profiles["farming"].duration_hours

    def test_no_daylight_room_raises(self, engine, location):
        """Farming needs four daylight hours; give the day only three."""
        payload = make_payload(hours=24)
        payload["hourly"]["is_day"] = [1 if 9 <= i <= 11 else 0 for i in range(24)]
        forecast = Forecast.from_api(payload, location)
        with pytest.raises(NoViableWindowError):
            engine.best_windows(forecast, date.today(), "farming")

    def test_too_few_hours_raises(self, engine, location):
        payload = make_payload(hours=2)
        forecast = Forecast.from_api(payload, location)
        with pytest.raises(NoViableWindowError):
            engine.best_windows(forecast, date.today(), "farming")

    def test_unknown_activity_is_a_validation_error(self, engine, forecast):
        with pytest.raises(UnknownActivityError) as info:
            engine.best_windows(forecast, date.today(), "quidditch")
        assert "football" in info.value.hint


class TestBuildPlan:
    def test_produces_a_complete_plan_without_ai(self, engine, forecast):
        plan = engine.build_plan(forecast, date.today(), "football")
        assert plan.activity_label == "Football"
        assert plan.headline
        assert plan.explanation
        assert plan.advice
        assert plan.checklist
        assert plan.windows
        assert plan.ai_used is False
        assert "unavailable" in plan.ai_note.lower()

    def test_no_viable_window_does_not_kill_the_plan(self, engine, location):
        payload = make_payload(hours=24)
        payload["hourly"]["is_day"] = [0] * 24
        forecast = Forecast.from_api(payload, location)
        plan = engine.build_plan(forecast, date.today(), "farming")
        assert plan.windows == []
        assert "daylight" in plan.timing_note.lower()
        assert plan.checklist  # the rest of the plan is still there

    def test_plan_round_trips_through_json(self, engine, forecast):
        from core.models import ActivityPlan

        plan = engine.build_plan(forecast, date.today(), "picnic")
        restored = ActivityPlan.from_dict(plan.to_dict())
        assert restored.activity_key == plan.activity_key
        assert restored.assessment.score == pytest.approx(plan.assessment.score)
        assert len(restored.windows) == len(plan.windows)
        assert [c.item for c in restored.checklist] == [c.item for c in plan.checklist]

    def test_text_export_contains_the_essentials(self, engine, forecast):
        text = engine.build_plan(forecast, date.today(), "picnic").to_text()
        assert "PICNIC PLAN" in text
        assert "VERDICT:" in text
        assert "PACKING CHECKLIST" in text


class TestRuleBasedContent:
    def test_hot_day_earns_water_and_a_timing_warning(self, engine, location):
        payload = make_payload(temp=34, feels=37, humidity=70)
        forecast = Forecast.from_api(payload, location)
        plan = engine.build_plan(forecast, date.today(), "farming")
        items = " ".join(c.item.lower() for c in plan.checklist)
        assert "water" in items
        assert any("morning" in a.lower() or "shade" in a.lower() for a in plan.advice)

    def test_wet_day_earns_a_waterproof(self, engine, location):
        payload = make_payload(prob=85, precip=4.0, code=63)
        forecast = Forecast.from_api(payload, location)
        plan = engine.build_plan(forecast, date.today(), "picnic")
        items = " ".join(c.item.lower() for c in plan.checklist)
        assert "waterproof" in items

    def test_storm_day_advises_shelter(self, engine, storm_payload, location):
        forecast = Forecast.from_api(storm_payload, location)
        plan = engine.build_plan(forecast, date.today(), "outdoor_event")
        assert plan.band is RiskBand.AVOID
        assert any("lightning" in a.lower() or "storm" in a.lower() for a in plan.advice)

    def test_pleasant_day_still_gives_advice(self, engine, forecast):
        plan = engine.build_plan(forecast, date.today(), "jogging")
        assert plan.advice  # never an empty panel

    def test_checklist_is_capped(self, engine, location):
        # Pile on every condition at once.
        payload = make_payload(
            temp=34, feels=37, humidity=95, prob=95, precip=8, code=82,
            wind=50, gust=70, uv=11, visibility=300,
        )
        forecast = Forecast.from_api(payload, location)
        plan = engine.build_plan(forecast, date.today(), "outdoor_event")
        assert len(plan.checklist) <= 14
        assert len(plan.advice) <= 6


class FakeAI:
    """Stand-in for GeminiClient -- the engine only needs two attributes."""

    available = True

    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = 0

    def explain(self, context):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


class TestAIIntegration:
    def test_ai_wording_replaces_the_rule_based_text(self, analyzer, profiles, forecast):
        from core.recommendation import RecommendationEngine

        ai = FakeAI(
            AIExplanation(
                verdict="Manageable",
                headline="Play early, it gets hot",
                explanation="The afternoon climbs past what is comfortable.",
                safety_advice=["Start before 09:00"],
                packing=[ChecklistItem(item="Ice towel", reason="Heat")],
                timing_note="Mornings are clearly better.",
            )
        )
        engine = RecommendationEngine(analyzer, profiles, ai_client=ai)
        plan = engine.build_plan(forecast, date.today(), "football")

        assert ai.calls == 1
        assert plan.ai_used is True
        assert plan.headline == "Play early, it gets hot"
        assert plan.advice == ["Start before 09:00"]
        assert "Ice towel" in [c.item for c in plan.checklist]
        # the rule-based essentials survive the merge
        assert "Studded boots" in [c.item for c in plan.checklist]

    def test_ai_failure_falls_back_silently(self, analyzer, profiles, forecast):
        from core.recommendation import RecommendationEngine

        ai = FakeAI(error=AIUnavailableError("The AI service is not responding."))
        engine = RecommendationEngine(analyzer, profiles, ai_client=ai)
        plan = engine.build_plan(forecast, date.today(), "football")

        assert plan.ai_used is False
        assert plan.headline and plan.explanation and plan.advice
        assert plan.ai_note == "The AI service is not responding."

    def test_an_unexpected_ai_crash_is_contained(self, analyzer, profiles, forecast):
        from core.recommendation import RecommendationEngine

        ai = FakeAI(error=ZeroDivisionError("boom"))
        engine = RecommendationEngine(analyzer, profiles, ai_client=ai)
        plan = engine.build_plan(forecast, date.today(), "football")

        assert plan.ai_used is False
        assert plan.checklist  # the plan is complete regardless
