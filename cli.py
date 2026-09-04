"""
Command-line front end -- the same core services the GUI uses.

    python cli.py Ibadan football
    python cli.py "Port Harcourt" picnic --date 2026-09-05 --save
    python cli.py --activities

It exists for two reasons: it was the Phase 4 proof that the product works
before any window existed, and it is the fastest way to check a change to the
scoring rules without clicking through the interface.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

# The plan text uses en dashes and degree signs, which the default Windows
# console codepage (cp1252) cannot encode. Ask politely for UTF-8; shrug if the
# terminal refuses, because a printing quirk must not crash the program.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError, ValueError):  # pragma: no cover
    pass

import config
from core.exceptions import WeatherAppError
from services import build_services


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Weather risk verdict for an outdoor activity."
    )
    parser.add_argument("location", nargs="?", help="Town or city, or 'lat, lon'")
    parser.add_argument("activity", nargs="?", help="Activity key, e.g. football")
    parser.add_argument("--date", dest="day", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--save", action="store_true", help="Save the plan to data/plans/")
    parser.add_argument("--no-ai", action="store_true", help="Skip the Gemini call")
    parser.add_argument("--activities", action="store_true", help="List activity keys and exit")
    args = parser.parse_args(argv)

    config.setup_logging()

    try:
        services = build_services(use_ai=not args.no_ai)
    except WeatherAppError as exc:
        print(f"Error: {exc.user_message}\n{exc.hint}", file=sys.stderr)
        return 2

    if args.activities:
        print("Available activities:\n")
        for key, profile in services.profiles.items():
            daylight = "daylight only" if profile.daylight_only else "any hour"
            print(f"  {key:<15} {profile.label:<16} {profile.duration_hours}h, {daylight}")
        return 0

    if not args.location or not args.activity:
        parser.print_help()
        return 1

    try:
        from core.validators import parse_date

        today = date.today()
        day = (
            parse_date(args.day, earliest=today, latest=today + timedelta(days=config.FORECAST_DAYS - 1))
            if args.day
            else today
        )

        locations = services.client.geocode(args.location)
        location = locations[0]
        print(f"Location: {location.display_name}  ({location.latitude:.4f}, {location.longitude:.4f})")

        forecast = services.client.fetch_forecast(location)
        if forecast.from_cache:
            print(f"(using a cached forecast from {forecast.fetched_at:%H:%M})")

        plan = services.engine.build_plan(forecast, day, args.activity)
        print()
        print(plan.to_text())

        services.storage.record_search(plan)
        if args.save:
            path = services.storage.save_plan(plan)
            print(f"\nSaved to {path}")
        return 0

    except WeatherAppError as exc:
        print(f"\n{exc.user_message}", file=sys.stderr)
        if exc.hint:
            print(exc.hint, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
