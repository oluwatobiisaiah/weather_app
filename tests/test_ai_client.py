"""
The AI layer, offline.

The important test in this file is `test_computed_band_wins`: the model is not
allowed to change the verdict, and this is what proves it.
"""

from __future__ import annotations

import json

import pytest
import requests

from core.ai_client import GeminiClient
from core.exceptions import AIResponseError, AIUnavailableError

GOOD_BODY = {
    "verdict": "Manageable",
    "headline": "Fine early, hot by three",
    "explanation": "It feels like 33 °C after midday, so play in the morning.",
    "safety_advice": ["Start before 09:00", "Drink every 15 minutes"],
    "packing": [
        {"item": "Ice towel", "reason": "Heat scored 68", "essential": True},
        {"item": "Extra water", "reason": "Two hours in the sun"},
    ],
    "timing_note": "Mornings are clearly better today.",
}

CONTEXT = {
    "location": "Ibadan, Nigeria",
    "activity": "Football",
    "day": "2026-09-05",
    "band": "Manageable",
    "score": 38.0,
    "factors": {"heat": 68, "rain": 36},
    "reasons": [],
    "windows": ["07:00–09:00; Safe (8/100)"],
    "hourly_lines": ["  10:00  28.0°C feels 31.0  UV 4  rain 20%  wind 9 km/h  Partly cloudy"],
}


def envelope(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


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
    def __init__(self, *responses):
        self.queue = list(responses)
        self.calls = 0
        self.last_body = None
        self.last_headers = None

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls += 1
        self.last_body = json
        self.last_headers = headers
        item = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        if isinstance(item, Exception):
            raise item
        return item


def client(*responses, key="AIza" + "x" * 35) -> GeminiClient:
    return GeminiClient(api_key=key, session=FakeSession(*responses))


class TestAvailability:
    def test_no_key_is_a_normal_state(self):
        ai = GeminiClient(api_key="")
        assert ai.available is False
        with pytest.raises(AIUnavailableError) as info:
            ai.explain(CONTEXT)
        assert "GEMINI_API_KEY" in info.value.hint

    def test_key_shape_is_checked(self):
        assert GeminiClient(api_key="AIza" + "x" * 35).key_looks_valid() is True
        assert GeminiClient(api_key="oops").key_looks_valid() is False


class TestParsing:
    def test_reads_a_clean_response(self):
        ai = client(FakeResponse(payload=envelope(json.dumps(GOOD_BODY))))
        result = ai.explain(CONTEXT)
        assert result.headline == "Fine early, hot by three"
        assert len(result.safety_advice) == 2
        assert result.packing[0].item == "Ice towel"
        assert result.packing[0].essential is True

    def test_strips_a_code_fence(self):
        fenced = "```json\n" + json.dumps(GOOD_BODY) + "\n```"
        ai = client(FakeResponse(payload=envelope(fenced)))
        assert ai.explain(CONTEXT).headline == "Fine early, hot by three"

    def test_plain_string_packing_items_are_accepted(self):
        body = dict(GOOD_BODY, packing=["Sun hat", "Water"])
        ai = client(FakeResponse(payload=envelope(json.dumps(body))))
        assert [c.item for c in ai.explain(CONTEXT).packing] == ["Sun hat", "Water"]

    def test_lists_are_bounded(self):
        body = dict(
            GOOD_BODY,
            safety_advice=[f"tip {i}" for i in range(30)],
            packing=[{"item": f"thing {i}"} for i in range(30)],
        )
        ai = client(FakeResponse(payload=envelope(json.dumps(body))))
        result = ai.explain(CONTEXT)
        assert len(result.safety_advice) == 6
        assert len(result.packing) == 12

    def test_invalid_json_is_an_ai_response_error(self):
        ai = client(FakeResponse(payload=envelope("sorry, I can't do that")))
        with pytest.raises(AIResponseError):
            ai.explain(CONTEXT)

    def test_missing_required_keys_is_an_error(self):
        ai = client(FakeResponse(payload=envelope(json.dumps({"verdict": "Safe"}))))
        with pytest.raises(AIResponseError) as info:
            ai.explain(CONTEXT)
        assert "headline" in str(info.value)

    def test_missing_candidates_is_an_error(self):
        ai = client(FakeResponse(payload={"promptFeedback": {"blockReason": "SAFETY"}}))
        with pytest.raises(AIResponseError) as info:
            ai.explain(CONTEXT)
        assert "SAFETY" in str(info.value)

    def test_non_json_body_is_an_error(self):
        ai = client(FakeResponse(text="<html>"))
        with pytest.raises(AIResponseError):
            ai.explain(CONTEXT)


class TestTheBandIsNotNegotiable:
    def test_computed_band_wins(self, caplog):
        """The model says Safe; the rules said Manageable. The rules win."""
        body = dict(GOOD_BODY, verdict="Safe")
        ai = client(FakeResponse(payload=envelope(json.dumps(body))))
        result = ai.explain(CONTEXT)
        assert result.verdict == "Manageable"
        assert result.headline == "Fine early, hot by three"  # the prose is kept

    def test_the_prompt_states_the_band_as_final(self):
        ai = client(FakeResponse(payload=envelope(json.dumps(GOOD_BODY))))
        prompt = ai.build_prompt(CONTEXT)
        assert "ALREADY been computed and is final" in prompt
        assert "Manageable" in prompt
        assert "do not change it" in prompt.lower()

    def test_the_prompt_carries_the_real_numbers(self):
        ai = client(FakeResponse(payload=envelope(json.dumps(GOOD_BODY))))
        prompt = ai.build_prompt(CONTEXT)
        assert "heat" in prompt and "68" in prompt
        assert "07:00–09:00" in prompt


class TestTransport:
    def test_key_travels_in_a_header_not_the_url(self):
        session = FakeSession(FakeResponse(payload=envelope(json.dumps(GOOD_BODY))))
        ai = GeminiClient(api_key="AIza" + "x" * 35, session=session)
        ai.explain(CONTEXT)
        assert session.last_headers["x-goog-api-key"].startswith("AIza")

    def test_asks_for_structured_output(self):
        session = FakeSession(FakeResponse(payload=envelope(json.dumps(GOOD_BODY))))
        ai = GeminiClient(api_key="AIza" + "x" * 35, session=session)
        ai.explain(CONTEXT)
        generation = session.last_body["generationConfig"]
        assert generation["responseMimeType"] == "application/json"
        assert generation["responseSchema"]["properties"]["verdict"]["enum"] == [
            "Safe", "Manageable", "Risky", "Avoid",
        ]

    def test_rejected_key_is_reported_clearly(self):
        ai = client(FakeResponse(status_code=403))
        with pytest.raises(AIUnavailableError) as info:
            ai.explain(CONTEXT)
        assert "key" in info.value.user_message.lower()

    def test_placeholder_key_400_explains_itself(self):
        """What an unfilled `.env` actually produces: a 400, not a 401."""
        ai = client(
            FakeResponse(
                status_code=400,
                payload={"error": {"message": "API key not valid. Please pass a valid API key."}},
            )
        )
        with pytest.raises(AIUnavailableError) as info:
            ai.explain(CONTEXT)
        assert "key was rejected" in info.value.user_message.lower()
        assert "API key not valid" in info.value.hint

    def test_other_400s_surface_the_services_own_wording(self):
        ai = client(
            FakeResponse(
                status_code=400,
                payload={"error": {"message": "Invalid JSON payload received."}},
            )
        )
        with pytest.raises(AIUnavailableError) as info:
            ai.explain(CONTEXT)
        assert "rejected the request" in info.value.user_message.lower()
        assert "Invalid JSON" in info.value.hint

    def test_rate_limit(self):
        ai = client(FakeResponse(status_code=429))
        with pytest.raises(AIUnavailableError):
            ai.explain(CONTEXT)

    def test_server_error_is_retried_once(self):
        session = FakeSession(
            FakeResponse(status_code=500),
            FakeResponse(payload=envelope(json.dumps(GOOD_BODY))),
        )
        ai = GeminiClient(api_key="AIza" + "x" * 35, session=session)
        assert ai.explain(CONTEXT).headline
        assert session.calls == 2

    def test_network_failure_is_unavailable_not_a_crash(self):
        ai = client(requests.exceptions.ConnectionError("offline"))
        with pytest.raises(AIUnavailableError):
            ai.explain(CONTEXT)
