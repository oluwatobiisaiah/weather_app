"""
The whole error hierarchy for the app.

One root exception means the UI needs exactly two handlers: `except
WeatherAppError` for everything it knows how to display, and `except Exception`
for the genuinely unexpected. Nothing in between, and no bare `except:`.

Every app exception carries two strings:

  user_message -- written for a person, shown in the dialog
  hint         -- an optional second line telling them what to do about it

The original library exception is kept as `__cause__` so the log still gets a
full traceback while the user gets a sentence.
"""

from __future__ import annotations


class WeatherAppError(Exception):
    """Root of every expected failure in this application."""

    def __init__(self, user_message: str, hint: str = "", *, cause: BaseException | None = None):
        super().__init__(user_message)
        self.user_message = user_message
        self.hint = hint
        if cause is not None:
            self.__cause__ = cause

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.user_message} {self.hint}".strip()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------
class ValidationError(WeatherAppError):
    """The user typed something the app cannot work with."""


class InvalidLocationError(ValidationError):
    """Empty, over-long, or non-place-looking location text."""


class InvalidDateTimeError(ValidationError):
    """Bad date/time format, or a date outside the forecast window."""


class UnknownActivityError(ValidationError):
    """An activity key that is not present in activities.json."""


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
class NetworkError(WeatherAppError):
    """The request never reached the service, or never came back."""


class ConnectionFailedError(NetworkError):
    """DNS failure, no route, Wi-Fi off."""


class RequestTimeoutError(NetworkError):
    """The service did not answer within the timeout."""


# ---------------------------------------------------------------------------
# API responses
# ---------------------------------------------------------------------------
class APIError(WeatherAppError):
    """The service answered, but not with what we needed."""


class LocationNotFoundError(APIError):
    """200 OK with zero results -- the place does not exist in the index."""


class RateLimitError(APIError):
    """HTTP 429."""


class ServiceUnavailableError(APIError):
    """HTTP 5xx, still failing after the retries."""


class MalformedResponseError(APIError):
    """Body was not JSON, or was JSON of an unexpected shape."""


# ---------------------------------------------------------------------------
# Data problems
# ---------------------------------------------------------------------------
class MissingDataError(WeatherAppError):
    """A required forecast series is absent or entirely null."""


class NoViableWindowError(WeatherAppError):
    """No slot of the required length fits the activity's constraints."""

    def __init__(self, activity: str, day, hint: str = ""):
        super().__init__(
            f"No suitable {activity} window fits inside daylight on {day}.",
            hint or "Try a shorter activity, or a different day.",
        )
        self.activity = activity
        self.day = day


# ---------------------------------------------------------------------------
# AI layer -- always caught internally, never shown as an error
# ---------------------------------------------------------------------------
class AIError(WeatherAppError):
    """Base for anything that goes wrong while asking Gemini for wording."""


class AIUnavailableError(AIError):
    """No key configured, network down, or the model service is failing."""


class AIResponseError(AIError):
    """The model answered with something that is not the agreed JSON."""


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
class StorageError(WeatherAppError):
    """Base for local file problems."""


class CorruptDataFileError(StorageError):
    """A JSON file on disk could not be parsed; it has been quarantined."""


class FileWriteError(StorageError):
    """The file could not be written (permissions, disk full, path gone)."""
