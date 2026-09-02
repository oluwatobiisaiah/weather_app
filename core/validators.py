"""
Every regular expression in the application lives here, and nowhere else.

Patterns are compiled once at import time and named in upper case. Each one has
a small helper wrapped around it, because a regex should decide *shape* while
Python decides *meaning* -- `ISO_DATE` proves "2026-02-31" looks like a date,
`datetime.strptime` proves it isn't one.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time

from core.exceptions import InvalidDateTimeError, InvalidLocationError

# ---------------------------------------------------------------------------
# Location text
# ---------------------------------------------------------------------------
#: Anything that is not a letter, digit, space, comma, dot, apostrophe or hyphen.
CLEAN_PUNCT = re.compile(r"[^\w\s,.'-]", re.UNICODE)

#: Runs of whitespace, including the tabs and newlines that come with a paste.
COLLAPSE_WS = re.compile(r"\s+")

#: A plausible place name: starts with a letter, 2-80 characters.
LOCATION_OK = re.compile(r"^[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ0-9 .,'-]{1,79}$")

#: "7.3776, 3.9059" -- lets a power user skip geocoding entirely.
COORD_PAIR = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")

# ---------------------------------------------------------------------------
# Date and time input
# ---------------------------------------------------------------------------
ISO_DATE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")

#: Accepts both "18:30" and "6:30 pm".
TIME_ANY = re.compile(
    r"^(?:([01]?\d|2[0-3]):([0-5]\d)|(0?[1-9]|1[0-2]):([0-5]\d)\s*([AaPp])[Mm])$"
)

DURATION = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(h|hr|hrs|hour|hours|m|min|mins|minute|minutes)\s*$",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Pulling numbers back out of prose
# ---------------------------------------------------------------------------
MEASUREMENT = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(°C|°F|km/h|mph|m/s|mm|%)", re.IGNORECASE
)

#: A model response that arrived fenced despite the JSON mime type.
CODE_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
SLUG = re.compile(r"[^a-z0-9]+")

#: Catch a truncated or quote-wrapped key at startup, not after a 400.
API_KEY_SHAPE = re.compile(r"^AIza[0-9A-Za-z_-]{35}$")


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------
def clean_location(raw: str) -> str:
    """
    Turn messy typed input into a searchable place name.

    '  ib@adan!!  ' -> 'Ibadan'
    'abuja, ng'     -> 'Abuja, Ng'   (see title_place for the NG case)

    Raises InvalidLocationError for empty or clearly non-place input.
    """
    if not raw or not raw.strip():
        raise InvalidLocationError(
            "Enter a town or city name.",
            hint="For example: Abuja, Ibadan or Port Harcourt",
        )

    text = CLEAN_PUNCT.sub("", raw)
    text = COLLAPSE_WS.sub(" ", text).strip(" ,.-")

    if not LOCATION_OK.match(text):
        raise InvalidLocationError(
            f"{raw.strip()!r} does not look like a place name.",
            hint="Letters, spaces, hyphens and commas only",
        )
    # Keep "NG" and "UK" as typed, fix "port harcourt" -> "Port Harcourt".
    return " ".join(w if w.isupper() else w.capitalize() for w in text.split())


def is_confident_match(query: str, resolved_name: str) -> bool:
    """
    Is the geocoder's top result plainly the place that was typed?

    The geocoder matches near-misses happily: "Lagoss" comes back as Lagossa in
    Tanzania -- a real place, so not an error, and not what anyone meant. When
    this returns False the UI asks before going ahead.
    """
    try:
        typed = clean_location(query).lower()
    except InvalidLocationError:
        return True  # already rejected upstream; do not second-guess it
    # "abuja" matches "Abuja"; "abuja, ng" matches through its first part.
    first_part = typed.split(",")[0].strip()
    return resolved_name.lower() in (typed, first_part)


def parse_coordinates(raw: str) -> tuple[float, float] | None:
    """Return (lat, lon) when the text is a coordinate pair, else None."""
    if not raw:
        return None
    match = COORD_PAIR.match(raw)
    if not match:
        return None
    lat, lon = float(match.group(1)), float(match.group(2))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise InvalidLocationError(
            f"{raw.strip()!r} is not a valid coordinate pair.",
            hint="Latitude -90 to 90, longitude -180 to 180",
        )
    return lat, lon


# ---------------------------------------------------------------------------
# Date and time helpers
# ---------------------------------------------------------------------------
def parse_date(raw: str, *, earliest: date | None = None, latest: date | None = None) -> date:
    """
    Validate 'YYYY-MM-DD' and return a real date.

    The regex proves the shape; strptime proves the day exists; the optional
    bounds prove it is inside the forecast window.
    """
    if not raw or not raw.strip():
        raise InvalidDateTimeError("Pick a date.", hint="Format: YYYY-MM-DD")

    text = raw.strip()
    if not ISO_DATE.match(text):
        raise InvalidDateTimeError(
            f"{text!r} is not a valid date.", hint="Use the format YYYY-MM-DD, e.g. 2026-09-05"
        )
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise InvalidDateTimeError(
            f"{text!r} is not a real calendar date.", hint="Check the day and month", cause=exc
        ) from exc

    if earliest and parsed < earliest:
        raise InvalidDateTimeError(
            "That date is in the past.", hint=f"The forecast starts on {earliest}"
        )
    if latest and parsed > latest:
        raise InvalidDateTimeError(
            "That date is beyond the forecast range.",
            hint=f"Forecasts are only available up to {latest}",
        )
    return parsed


def parse_time(raw: str) -> time:
    """'18:30' or '6:30 pm' -> datetime.time(18, 30)."""
    if not raw or not raw.strip():
        raise InvalidDateTimeError("Enter a time.", hint="For example 18:30 or 6:30 pm")

    match = TIME_ANY.match(raw.strip())
    if not match:
        raise InvalidDateTimeError(
            f"{raw.strip()!r} is not a valid time.", hint="Use 18:30 or 6:30 pm"
        )

    h24, m24, h12, m12, meridiem = match.groups()
    if h24 is not None:
        return time(int(h24), int(m24))

    hour = int(h12) % 12
    if meridiem.lower() == "p":
        hour += 12
    return time(hour, int(m12))


def parse_duration(raw: str) -> float:
    """'90 mins' -> 1.5, '2h' -> 2.0. Returns hours as a float."""
    if not raw or not raw.strip():
        raise InvalidDateTimeError("Enter a duration.", hint="For example 2h or 90 mins")

    match = DURATION.match(raw)
    if not match:
        raise InvalidDateTimeError(
            f"{raw.strip()!r} is not a duration.", hint="Try '2h', '90 mins' or '1.5 hours'"
        )

    amount = float(match.group(1))
    unit = match.group(2).lower()
    hours = amount / 60 if unit.startswith("m") and unit != "h" else amount
    if not 0 < hours <= 24:
        raise InvalidDateTimeError(
            "Duration must be between a few minutes and 24 hours.", hint=f"Got {hours:g} hours"
        )
    return hours


# ---------------------------------------------------------------------------
# Prose helpers
# ---------------------------------------------------------------------------
def extract_measurements(text: str) -> list[tuple[float, str]]:
    """
    Pull every number-with-unit out of weather or AI prose.

    'Feels like 36.2 °C with 12 km/h wind' -> [(36.2, '°C'), (12.0, 'km/h')]

    Used to sanity-check that the wording Gemini produced matches the figures
    that were actually sent to it.
    """
    if not text:
        return []
    return [(float(value), unit) for value, unit in MEASUREMENT.findall(text)]


def strip_code_fence(text: str) -> str:
    """Unwrap ```json ... ``` if the model added a fence anyway."""
    if not text:
        return ""
    match = CODE_FENCE.match(text.strip())
    return match.group(1) if match else text.strip()


def slugify(text: str) -> str:
    """'Port Harcourt' -> 'port-harcourt'. Safe for filenames on any OS."""
    return SLUG.sub("-", (text or "").lower()).strip("-") or "untitled"


def looks_like_api_key(key: str) -> bool:
    """Shape check only -- a real key is still proven by the first call."""
    return bool(key and API_KEY_SHAPE.match(key.strip()))
