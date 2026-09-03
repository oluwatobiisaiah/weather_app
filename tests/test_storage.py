"""File handling: atomic writes, corrupt-file recovery, caps, and the cache."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import pytest
from conftest import make_payload

from core.exceptions import CorruptDataFileError, FileWriteError
from core.models import Forecast, Location
from core.recommendation import RecommendationEngine


@pytest.fixture
def location() -> Location:
    return Location(name="Ibadan", latitude=7.3776, longitude=3.9059, country="Nigeria")


@pytest.fixture
def plan(analyzer, profiles, payload, location):
    engine = RecommendationEngine(analyzer, profiles)
    return engine.build_plan(Forecast.from_api(payload, location), date.today(), "football")


class TestAtomicWrite:
    def test_leaves_no_temporary_file_behind(self, storage, location):
        storage.add_favourite(location)
        assert list(storage.data_dir.glob("*.tmp")) == []

    def test_written_file_is_valid_utf8_json(self, storage):
        storage.add_favourite(Location(name="Ilé-Ifẹ̀", latitude=7.5, longitude=4.5))
        text = storage.favourites_file.read_text(encoding="utf-8")
        assert "Ilé-Ifẹ̀" in text  # ensure_ascii=False keeps the real characters
        assert json.loads(text)

    def test_unwritable_path_raises_a_readable_error(self, storage, location, monkeypatch):
        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("os.replace", boom)
        with pytest.raises(FileWriteError) as info:
            storage.add_favourite(location)
        assert "favourites" in info.value.user_message.lower()


class TestCorruptFiles:
    def test_damaged_file_is_quarantined(self, storage):
        storage.favourites_file.write_text("this is not json", encoding="utf-8")

        with pytest.raises(CorruptDataFileError) as info:
            storage.list_favourites()

        assert "damaged" in info.value.user_message
        assert not storage.favourites_file.exists()
        quarantined = list(storage.data_dir.glob("favourites.corrupt-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_text(encoding="utf-8") == "this is not json"

    def test_the_app_continues_after_the_quarantine(self, storage, location):
        storage.favourites_file.write_text("{{{", encoding="utf-8")
        with pytest.raises(CorruptDataFileError):
            storage.list_favourites()
        # Second read starts clean rather than raising forever.
        assert storage.list_favourites() == []
        storage.add_favourite(location)
        assert len(storage.list_favourites()) == 1

    def test_recording_a_search_survives_a_damaged_history(self, storage, plan):
        storage.history_file.write_text("nope", encoding="utf-8")
        history = storage.record_search(plan)  # must not raise
        assert len(history) == 1

    def test_a_missing_file_is_not_an_error(self, storage):
        assert storage.list_favourites() == []
        assert storage.recent_searches() == []


class TestFavourites:
    def test_deduplicates_by_rounded_coordinates(self, storage):
        storage.add_favourite(Location(name="Ibadan", latitude=7.3776, longitude=3.9059))
        storage.add_favourite(Location(name="Ibadan", latitude=7.3778, longitude=3.9061))
        assert len(storage.list_favourites()) == 1

    def test_newest_first(self, storage):
        storage.add_favourite(Location(name="Lagos", latitude=6.5, longitude=3.4))
        storage.add_favourite(Location(name="Kano", latitude=12.0, longitude=8.5))
        assert storage.list_favourites()[0]["name"] == "Kano"

    def test_is_favourite_and_remove(self, storage, location):
        assert storage.is_favourite(location) is False
        storage.add_favourite(location)
        assert storage.is_favourite(location) is True
        storage.remove_favourite(location.key())
        assert storage.is_favourite(location) is False

    def test_capped(self, storage):
        import config

        for i in range(config.FAVOURITES_LIMIT + 8):
            storage.add_favourite(Location(name=f"Place {i}", latitude=i * 0.5, longitude=i))
        assert len(storage.list_favourites()) == config.FAVOURITES_LIMIT


class TestHistory:
    def test_repeat_search_moves_up_instead_of_duplicating(self, storage, plan):
        storage.record_search(plan)
        storage.record_search(plan)
        assert len(storage.recent_searches()) == 1

    def test_records_the_verdict(self, storage, plan):
        entry = storage.record_search(plan)[0]
        assert entry["band"] == plan.band.value
        assert entry["activity_key"] == "football"

    def test_capped_at_the_limit(self, storage, plan):
        import config

        for i in range(config.HISTORY_LIMIT + 5):
            plan.location = Location(name=f"P{i}", latitude=i * 0.4, longitude=i * 0.3)
            storage.record_search(plan)
        assert len(storage.recent_searches(limit=999)) == config.HISTORY_LIMIT


class TestPlans:
    def test_saves_json_and_printable_text(self, storage, plan):
        path = storage.save_plan(plan)
        assert path.exists()
        assert path.with_suffix(".txt").exists()
        assert "FOOTBALL PLAN" in path.with_suffix(".txt").read_text(encoding="utf-8")

    def test_round_trips(self, storage, plan):
        plan.checklist[0].checked = True
        storage.save_plan(plan)
        restored = storage.load_plan(storage.plan_filename(plan) + ".json")
        assert restored.activity_key == plan.activity_key
        assert restored.checklist[0].checked is True

    def test_listing_includes_the_band(self, storage, plan):
        storage.save_plan(plan)
        entries = storage.list_plans()
        assert len(entries) == 1
        assert entries[0]["band"] == plan.band.value

    def test_export_writes_anywhere(self, storage, plan, tmp_path):
        target = tmp_path / "my-plan.txt"
        storage.export_plan_text(plan, target)
        assert "VERDICT:" in target.read_text(encoding="utf-8")


class TestCache:
    def test_fresh_entry_comes_back(self, storage, payload):
        storage.cache_put("7.38_3.91", payload)
        result = storage.cache_get("7.38_3.91")
        assert result is not None
        cached, fetched_at = result
        assert cached["hourly"]["time"] == payload["hourly"]["time"]
        assert isinstance(fetched_at, datetime)

    def test_expired_entry_is_withheld_unless_asked_for(self, storage, payload):
        storage.cache_put("key", payload)
        raw = json.loads(storage._cache_path("key").read_text(encoding="utf-8"))
        raw["fetched_at"] = (datetime.now() - timedelta(hours=6)).isoformat()
        storage._cache_path("key").write_text(json.dumps(raw), encoding="utf-8")

        assert storage.cache_get("key") is None
        assert storage.cache_get("key", allow_stale=True) is not None

    def test_missing_entry_is_none(self, storage):
        assert storage.cache_get("nothing-here") is None

    def test_a_damaged_cache_entry_is_ignored_not_raised(self, storage):
        storage._cache_path("key").write_text("garbage", encoding="utf-8")
        assert storage.cache_get("key") is None

    def test_clear(self, storage, payload):
        storage.cache_put("a", payload)
        storage.cache_put("b", payload)
        assert storage.clear_cache() == 2


class TestActivities:
    def test_loads_every_profile(self, storage):
        profiles = storage.load_activities()
        assert set(profiles) == {
            "football", "jogging", "farming", "picnic", "travelling", "outdoor_event",
        }
        assert profiles["farming"].duration_hours == 4
        assert profiles["farming"].daylight_only is True

    def test_missing_file_is_reported_clearly(self, storage):
        storage.activities_file.unlink()
        with pytest.raises(Exception) as info:
            storage.load_activities()
        assert "activity" in str(info.value).lower()
