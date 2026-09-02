"""
StorageManager -- every read and write the app performs on the local disk.

One strategy throughout: JSON, UTF-8, `ensure_ascii=False`, written atomically
and read defensively.

*Atomic* matters because the failure it prevents is real: the app is killed
halfway through `json.dump` and the user's favourites are now half a file.
Writing to a temporary file in the same directory and then calling `os.replace`
makes the swap atomic on both Windows and POSIX.

*Defensive* matters because a file that has been hand-edited or truncated must
not stop the app: it is quarantined with a timestamp, the user is told once,
and a fresh default takes its place.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import config
from core.exceptions import CorruptDataFileError, FileWriteError, StorageError
from core.models import ActivityPlan, ActivityProfile, Location
from core.validators import slugify

log = logging.getLogger(__name__)


class StorageManager:
    """Favourites, search history, saved plans, and the forecast cache."""

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        cache_minutes: int | None = None,
    ):
        self.data_dir = Path(data_dir) if data_dir else config.DATA_DIR
        self.plans_dir = self.data_dir / "plans"
        self.cache_dir = self.data_dir / "cache"
        self.favourites_file = self.data_dir / "favourites.json"
        self.history_file = self.data_dir / "search_history.json"
        self.activities_file = self.data_dir / "activities.json"
        self.cache_minutes = (
            cache_minutes if cache_minutes is not None else config.CACHE_MINUTES
        )
        for folder in (self.data_dir, self.plans_dir, self.cache_dir):
            folder.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Low-level file access
    # -----------------------------------------------------------------------
    def _write_json(self, path: Path, payload: Any) -> None:
        """Write `payload` to `path` atomically, or raise FileWriteError."""
        tmp = path.with_suffix(path.suffix + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)  # atomic swap
        except OSError as exc:
            raise FileWriteError(
                f"Could not save {path.name}.",
                hint="Check that the folder exists and is writable.",
                cause=exc,
            ) from exc
        finally:
            tmp.unlink(missing_ok=True)

    def _read_json(self, path: Path, default: Any) -> Any:
        """
        Read JSON, tolerating a first run and surviving a damaged file.

        A missing file is normal and returns the default. A damaged file is
        quarantined and reported once, so the user knows why their favourites
        vanished instead of silently losing them.
        """
        try:
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            backup = path.with_suffix(f".corrupt-{int(time.time())}")
            try:
                path.rename(backup)
            except OSError:
                backup = path
            log.warning("Corrupt %s quarantined as %s", path.name, backup.name)
            raise CorruptDataFileError(
                f"Your {path.stem.replace('_', ' ')} file was damaged and has been reset.",
                hint=f"The damaged copy was kept as {backup.name}",
                cause=exc,
            ) from exc
        except OSError as exc:
            raise StorageError(
                f"Could not open {path.name}.", hint="Check file permissions.", cause=exc
            ) from exc

    # -----------------------------------------------------------------------
    # Activity profiles
    # -----------------------------------------------------------------------
    def load_activities(self) -> dict[str, ActivityProfile]:
        """Read data/activities.json into ActivityProfile objects."""
        raw = self._read_json(self.activities_file, None)
        if not raw:
            raise StorageError(
                "The activity settings file is missing.",
                hint=f"Expected to find {self.activities_file.name} in the data folder.",
            )
        return {key: ActivityProfile.from_dict(key, value) for key, value in raw.items()}

    # -----------------------------------------------------------------------
    # Favourites
    # -----------------------------------------------------------------------
    def list_favourites(self) -> list[dict]:
        data = self._read_json(self.favourites_file, [])
        return data if isinstance(data, list) else []

    def is_favourite(self, location: Location) -> bool:
        key = location.key()
        return any(entry.get("key") == key for entry in self.list_favourites())

    def add_favourite(self, location: Location) -> list[dict]:
        """Add a location, newest first, de-duplicated by rounded coordinates."""
        favourites = [e for e in self.list_favourites() if e.get("key") != location.key()]
        entry = location.to_dict()
        entry["key"] = location.key()
        entry["display_name"] = location.display_name
        entry["added_at"] = datetime.now().isoformat(timespec="seconds")
        favourites.insert(0, entry)
        favourites = favourites[: config.FAVOURITES_LIMIT]
        self._write_json(self.favourites_file, favourites)
        return favourites

    def remove_favourite(self, key: str) -> list[dict]:
        favourites = [e for e in self.list_favourites() if e.get("key") != key]
        self._write_json(self.favourites_file, favourites)
        return favourites

    # -----------------------------------------------------------------------
    # Search history
    # -----------------------------------------------------------------------
    def recent_searches(self, limit: int = 20) -> list[dict]:
        data = self._read_json(self.history_file, [])
        return data[:limit] if isinstance(data, list) else []

    def record_search(self, plan: ActivityPlan) -> list[dict]:
        """
        Append a search, capped and de-duplicated.

        Repeating a search moves the existing entry to the top rather than
        filling the list with copies of the same query.
        """
        try:
            history = self._read_json(self.history_file, [])
        except CorruptDataFileError:
            history = []  # already quarantined; recording must not fail the search
        if not isinstance(history, list):
            history = []

        signature = f"{plan.location.key()}|{plan.activity_key}|{plan.day.isoformat()}"
        history = [e for e in history if e.get("signature") != signature]
        history.insert(
            0,
            {
                "signature": signature,
                "location": plan.location.to_dict(),
                "display_name": plan.location.display_name,
                "activity_key": plan.activity_key,
                "activity_label": plan.activity_label,
                "day": plan.day.isoformat(),
                "band": plan.band.value,
                "score": round(plan.assessment.score, 1),
                "searched_at": datetime.now().isoformat(timespec="seconds"),
            },
        )
        history = history[: config.HISTORY_LIMIT]
        self._write_json(self.history_file, history)
        return history

    def clear_history(self) -> None:
        self._write_json(self.history_file, [])

    # -----------------------------------------------------------------------
    # Saved plans
    # -----------------------------------------------------------------------
    def plan_filename(self, plan: ActivityPlan) -> str:
        return f"{slugify(plan.location.name)}-{slugify(plan.activity_key)}-{plan.day.isoformat()}"

    def save_plan(self, plan: ActivityPlan) -> Path:
        """Write the plan as JSON, plus a printable .txt beside it."""
        stem = self.plan_filename(plan)
        json_path = self.plans_dir / f"{stem}.json"
        text_path = self.plans_dir / f"{stem}.txt"
        self._write_json(json_path, plan.to_dict())
        try:
            text_path.write_text(plan.to_text(), encoding="utf-8")
        except OSError as exc:
            raise FileWriteError(
                f"Saved the plan, but could not write {text_path.name}.", cause=exc
            ) from exc
        return json_path

    def list_plans(self) -> list[dict]:
        entries: list[dict] = []
        for path in sorted(self.plans_dir.glob("*.json"), reverse=True):
            try:
                data = self._read_json(path, None)
            except StorageError:
                continue
            if not data:
                continue
            entries.append(
                {
                    "file": path.name,
                    "label": f"{data.get('activity_label', '?')} · "
                    f"{data.get('location', {}).get('name', '?')} · {data.get('day', '?')}",
                    "band": data.get("assessment", {}).get("band", ""),
                    "created_at": data.get("created_at", ""),
                }
            )
        return entries

    def load_plan(self, filename: str) -> ActivityPlan:
        data = self._read_json(self.plans_dir / filename, None)
        if not data:
            raise StorageError(f"Could not find the saved plan {filename}.")
        return ActivityPlan.from_dict(data)

    def export_plan_text(self, plan: ActivityPlan, path: Path) -> Path:
        try:
            Path(path).write_text(plan.to_text(), encoding="utf-8")
        except OSError as exc:
            raise FileWriteError(f"Could not write {Path(path).name}.", cause=exc) from exc
        return Path(path)

    # -----------------------------------------------------------------------
    # Forecast cache
    # -----------------------------------------------------------------------
    def _cache_path(self, key: str) -> Path:
        return self.cache_dir / f"{slugify(key)}.json"

    def cache_put(self, key: str, payload: dict) -> None:
        """Store a raw API payload. Cache failures are logged, never raised."""
        try:
            self._write_json(
                self._cache_path(key),
                {"fetched_at": datetime.now().isoformat(), "payload": payload},
            )
        except StorageError as exc:
            log.warning("Could not cache forecast for %s: %s", key, exc)

    def cache_get(self, key: str, *, allow_stale: bool = False) -> tuple[dict, datetime] | None:
        """
        Return (payload, fetched_at) when usable, else None.

        `allow_stale=True` is the offline path: an old forecast with a clear
        "this is stale" marker beats a blank screen.
        """
        path = self._cache_path(key)
        try:
            data = self._read_json(path, None)
        except StorageError:
            return None  # a bad cache entry is never worth a dialog
        if not data:
            return None

        try:
            fetched_at = datetime.fromisoformat(data["fetched_at"])
            payload = data["payload"]
        except (KeyError, TypeError, ValueError):
            return None

        age_minutes = (datetime.now() - fetched_at).total_seconds() / 60
        if not allow_stale and age_minutes > self.cache_minutes:
            return None
        return payload, fetched_at

    def clear_cache(self) -> int:
        removed = 0
        for path in self.cache_dir.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed
