"""
GeminiClient -- the narrator, not the judge.

The risk band has already been decided by ActivityRiskAnalyzer before this
module is called. Gemini receives that band as a stated fact and is asked to
explain it in plain language, add safety advice, and justify a packing list.

Three guards sit around the response:

  1. the fence stripper, in case the model wraps JSON in ```json anyway;
  2. a schema check, so a missing key becomes AIResponseError rather than a
     KeyError three layers away;
  3. a verdict cross-check -- if the model disagrees with the computed band,
     the computed band wins and the disagreement is logged.

Every failure in here is an AIError, which RecommendationEngine catches. The
user never sees an AI failure as an error; they see the rule-based wording.
"""

from __future__ import annotations

import json
import logging

import requests

import config
from core.exceptions import AIResponseError, AIUnavailableError
from core.models import AIExplanation, ChecklistItem
from core.validators import looks_like_api_key, strip_code_fence

log = logging.getLogger(__name__)

MAX_ADVICE = 6
MAX_PACKING = 12

RESPONSE_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "verdict": {"type": "STRING", "enum": ["Safe", "Manageable", "Risky", "Avoid"]},
        "headline": {"type": "STRING"},
        "explanation": {"type": "STRING"},
        "safety_advice": {"type": "ARRAY", "items": {"type": "STRING"}},
        "packing": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "item": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                    "essential": {"type": "BOOLEAN"},
                },
                "required": ["item"],
            },
        },
        "timing_note": {"type": "STRING"},
    },
    "required": ["verdict", "headline", "explanation", "safety_advice", "packing"],
}


class GeminiClient:
    """Wraps one `generateContent` call and the parsing around it."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str | None = None,
        timeout: int | None = None,
        session=None,
    ):
        self.api_key = (api_key if api_key is not None else config.gemini_api_key()).strip()
        self.model = model or config.GEMINI_MODEL
        self.timeout = timeout if timeout is not None else config.AI_TIMEOUT
        self.session = session or requests.Session()

    # -----------------------------------------------------------------------
    @property
    def available(self) -> bool:
        """No key is a normal state, not an error -- the app runs without it."""
        return bool(self.api_key)

    def key_looks_valid(self) -> bool:
        return looks_like_api_key(self.api_key)

    # -----------------------------------------------------------------------
    def explain(self, context: dict) -> AIExplanation:
        """Ask for the wording. Raises AIUnavailableError / AIResponseError."""
        if not self.available:
            raise AIUnavailableError(
                "AI explanation unavailable showing standard guidance.",
                hint="Add GEMINI_API_KEY to the .env file to enable it.",
            )

        prompt = self.build_prompt(context)
        payload = self._call(prompt)
        data = self._parse(payload)
        return self._to_explanation(data, context)

    # -----------------------------------------------------------------------
    def build_prompt(self, context: dict) -> str:
        """The band is stated as final, up front, before anything else."""
        hourly_lines = "\n".join(context.get("hourly_lines", [])) or "  (no hourly detail)"
        flag_text = "\n".join(f"  - {f}" for f in (context.get("reasons") or ["none"]))
        windows = context.get("windows") or []
        window_text = (
            "\n".join(f"  {w}" for w in windows) if windows else "  (no suitable window found)"
        )
        factors = json.dumps(context.get("factors", {}), ensure_ascii=False)

        return f"""You are a cautious outdoor-safety briefer.

The risk verdict has ALREADY been computed and is final: {context['band']} \
(score {context['score']:.0f}/100). Explain that verdict; do not change it.

Location: {context['location']}
Activity: {context['activity']}
Date: {context['day']}

Computed factor scores (0 = perfect, 100 = dangerous): {factors}
Safety flags raised by the scoring rules:
{flag_text}

Best time windows found:
{window_text}

Hourly conditions:
{hourly_lines}

Write for a general audience in clear English. Be concrete about the numbers
above; quote them. Give at most {MAX_ADVICE} pieces of safety advice, each one
an action the person can take. Give at most {MAX_PACKING} packing items, and
every item must be justified by a condition listed above. Keep the explanation
to about 90 words. Do not invent weather figures that are not shown here."""

    # -----------------------------------------------------------------------
    def _call(self, prompt: str) -> dict:
        url = config.GEMINI_URL.format(model=self.model)
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.4,
                "responseMimeType": "application/json",
                "responseSchema": RESPONSE_SCHEMA,
            },
        }
        headers = {
            "Content-Type": "application/json",
            # A header, not a query string: query strings end up in logs.
            "x-goog-api-key": self.api_key,
        }

        for attempt in range(2):
            try:
                response = self.session.post(
                    url, json=body, headers=headers, timeout=self.timeout
                )
            except requests.exceptions.Timeout as exc:
                if attempt == 0:
                    continue
                raise AIUnavailableError(
                    "The AI service did not respond in time.", cause=exc
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise AIUnavailableError(
                    "Could not reach the AI service.", cause=exc
                ) from exc

            if response.status_code in (400, 401, 403):
                # 400 is what an unfilled placeholder key produces, so the
                # service's own wording is far more useful than the number.
                detail = self._error_detail(response)
                if "api key" in detail.lower() or response.status_code != 400:
                    raise AIUnavailableError(
                        "The AI key was rejected.",
                        hint=detail or "Check GEMINI_API_KEY in your .env file.",
                    )
                raise AIUnavailableError(
                    "The AI service rejected the request.", hint=detail
                )
            if response.status_code == 429:
                raise AIUnavailableError("The AI service is rate-limited right now.")
            if response.status_code >= 500 and attempt == 0:
                continue
            if response.status_code >= 400:
                raise AIUnavailableError(
                    f"The AI service returned error {response.status_code}.",
                    hint=self._error_detail(response),
                )

            try:
                return response.json()
            except ValueError as exc:
                raise AIResponseError(
                    "The AI service sent a reply that was not JSON.", cause=exc
                ) from exc

        raise AIUnavailableError("The AI service is not responding.")

    @staticmethod
    def _error_detail(response) -> str:
        """Pull the service's own explanation out of an error body."""
        try:
            body = response.json()
        except ValueError:
            return ""
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                return str(error.get("message", ""))
        return ""

    # -----------------------------------------------------------------------
    def _parse(self, payload: dict) -> dict:
        """Dig the text out of the candidate envelope, then parse it as JSON."""
        try:
            text = payload["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            blocked = (payload or {}).get("promptFeedback", {}).get("blockReason")
            if blocked:
                raise AIResponseError(f"The AI declined to answer ({blocked}).") from exc
            raise AIResponseError(
                "The AI reply was missing its content.", cause=exc
            ) from exc

        try:
            data = json.loads(strip_code_fence(text))
        except json.JSONDecodeError as exc:
            raise AIResponseError(
                "The AI reply was not valid JSON.", cause=exc
            ) from exc

        if not isinstance(data, dict):
            raise AIResponseError("The AI reply was not an object.")

        missing = [k for k in ("headline", "explanation") if not data.get(k)]
        if missing:
            raise AIResponseError(f"The AI reply was missing: {', '.join(missing)}.")
        return data

    # -----------------------------------------------------------------------
    def _to_explanation(self, data: dict, context: dict) -> AIExplanation:
        """Bound the lists, and let the computed band win any disagreement."""
        verdict = str(data.get("verdict", "")).strip()
        computed = context["band"]
        if verdict and verdict.lower() != computed.lower():
            log.warning(
                "Gemini returned verdict %r but the computed band is %r; keeping computed.",
                verdict,
                computed,
            )
            verdict = computed

        advice = [
            str(a).strip()
            for a in (data.get("safety_advice") or [])
            if str(a).strip()
        ][:MAX_ADVICE]

        packing: list[ChecklistItem] = []
        for raw in (data.get("packing") or [])[:MAX_PACKING]:
            if isinstance(raw, str):
                packing.append(ChecklistItem(item=raw.strip()))
            elif isinstance(raw, dict) and str(raw.get("item", "")).strip():
                packing.append(ChecklistItem.from_dict(raw))

        return AIExplanation(
            verdict=verdict or computed,
            headline=str(data["headline"]).strip(),
            explanation=str(data["explanation"]).strip(),
            safety_advice=advice,
            packing=packing,
            timing_note=str(data.get("timing_note", "") or "").strip(),
        )
