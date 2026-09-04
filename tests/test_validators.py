"""Every regex: things it must accept, and things it must refuse."""

from __future__ import annotations

from datetime import date, time

import pytest

from core.exceptions import InvalidDateTimeError, InvalidLocationError
from core.validators import (
    clean_location,
    extract_measurements,
    is_confident_match,
    looks_like_api_key,
    parse_coordinates,
    parse_date,
    parse_duration,
    parse_time,
    slugify,
    strip_code_fence,
)


class TestCleanLocation:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("  ib@adan!!  ", "Ibadan"),
            ("port harcourt", "Port Harcourt"),
            ("ile-ife", "Ile-ife"),
            ("abuja, NG", "Abuja, NG"),          # NG stays upper case
            ("  Lagos\t\nState ", "Lagos State"),
        ],
    )
    def test_accepts_and_cleans(self, raw, expected):
        assert clean_location(raw) == expected

    @pytest.mark.parametrize("raw", ["", "   ", "123", "!!!", "🌧️🌧️", "'; DROP TABLE--"])
    def test_rejects_junk(self, raw):
        with pytest.raises(InvalidLocationError):
            clean_location(raw)

    def test_error_carries_a_readable_message(self):
        with pytest.raises(InvalidLocationError) as info:
            clean_location("")
        assert "town or city" in info.value.user_message.lower()
        assert info.value.hint


class TestCoordinates:
    def test_parses_a_pair(self):
        assert parse_coordinates("7.3776, 3.9059") == (7.3776, 3.9059)

    def test_handles_negative_values(self):
        assert parse_coordinates(" -33.9, 18.4 ") == (-33.9, 18.4)

    def test_plain_names_are_not_coordinates(self):
        assert parse_coordinates("Ibadan") is None

    def test_out_of_range_is_refused(self):
        with pytest.raises(InvalidLocationError):
            parse_coordinates("95.0, 3.0")


class TestConfidentMatch:
    @pytest.mark.parametrize(
        "typed, resolved",
        [
            ("Ibadan", "Ibadan"),
            ("ibadan", "Ibadan"),
            ("  IBADAN  ", "IBADAN"),
            ("abuja, NG", "Abuja"),
            ("port harcourt", "Port Harcourt"),
        ],
    )
    def test_a_real_match_needs_no_confirmation(self, typed, resolved):
        assert is_confident_match(typed, resolved) is True

    @pytest.mark.parametrize(
        "typed, resolved",
        [
            ("Lagoss", "Lagossa"),      # the case that prompted this check
            ("Lagos", "Lagos Island"),
            ("Kano", "Kanoya"),
        ],
    )
    def test_a_near_miss_must_be_confirmed(self, typed, resolved):
        assert is_confident_match(typed, resolved) is False

    def test_input_that_never_validated_is_left_alone(self):
        # clean_location would reject this; the caller already handled it.
        assert is_confident_match("!!!", "Anywhere") is True


class TestParseDate:
    def test_accepts_iso(self):
        assert parse_date("2026-09-05") == date(2026, 9, 5)

    @pytest.mark.parametrize("raw", ["2026-13-01", "05/09/2026", "2026-9-5", "", "tomorrow"])
    def test_rejects_bad_shapes(self, raw):
        with pytest.raises(InvalidDateTimeError):
            parse_date(raw)

    def test_rejects_a_date_that_does_not_exist(self):
        # The regex is happy with 31 February; strptime is not.
        with pytest.raises(InvalidDateTimeError):
            parse_date("2026-02-31")

    def test_enforces_the_forecast_window(self):
        with pytest.raises(InvalidDateTimeError) as info:
            parse_date("2026-09-20", latest=date(2026, 9, 8))
        assert "2026-09-08" in info.value.hint


class TestParseTime:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("18:30", time(18, 30)),
            ("6:30 pm", time(18, 30)),
            ("6:30PM", time(18, 30)),
            ("06:05", time(6, 5)),
            ("12:30 am", time(0, 30)),
            ("12:30 pm", time(12, 30)),
        ],
    )
    def test_accepts_both_clocks(self, raw, expected):
        assert parse_time(raw) == expected

    @pytest.mark.parametrize("raw", ["25:00", "7:60", "half past six", ""])
    def test_rejects_nonsense(self, raw):
        with pytest.raises(InvalidDateTimeError):
            parse_time(raw)


class TestParseDuration:
    @pytest.mark.parametrize(
        "raw, expected",
        [("2h", 2.0), ("90 mins", 1.5), ("1.5 hours", 1.5), (" 45 min ", 0.75)],
    )
    def test_returns_hours(self, raw, expected):
        assert parse_duration(raw) == pytest.approx(expected)

    @pytest.mark.parametrize("raw", ["soon", "2 fortnights", "0h", "48h"])
    def test_rejects_the_rest(self, raw):
        with pytest.raises(InvalidDateTimeError):
            parse_duration(raw)


class TestProseHelpers:
    def test_extracts_numbers_with_units(self):
        text = "Feels like 36.2 °C with 12 km/h wind and a 70% chance of 4.5 mm of rain."
        assert extract_measurements(text) == [
            (36.2, "°C"),
            (12.0, "km/h"),
            (70.0, "%"),
            (4.5, "mm"),
        ]

    def test_handles_negative_temperatures(self):
        assert extract_measurements("down to -3.5 °C") == [(-3.5, "°C")]

    def test_empty_text_is_not_an_error(self):
        assert extract_measurements("") == []

    def test_strips_a_json_fence(self):
        assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_leaves_bare_json_alone(self):
        assert strip_code_fence('{"a": 1}') == '{"a": 1}'


class TestMisc:
    def test_slugify_builds_a_filename(self):
        assert slugify("Port Harcourt") == "port-harcourt"
        assert slugify("!!!") == "untitled"

    def test_api_key_shape(self):
        assert looks_like_api_key("AIza" + "b" * 35)
        assert not looks_like_api_key("AIzaTooShort")
        assert not looks_like_api_key("")
