"""
Entry point.

    python main.py             launch the window
    python main.py --no-ai     launch with Gemini disabled
    python cli.py <place> <activity>   the command-line version

Startup order matters: directories and logging first, so that anything which
goes wrong afterwards is written down somewhere the user can find it.
"""

from __future__ import annotations

import argparse
import sys
import traceback

import config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=config.APP_NAME)
    parser.add_argument("--no-ai", action="store_true", help="Run without the Gemini layer")
    args = parser.parse_args(argv)

    config.ensure_directories()
    log = config.setup_logging()
    log.info("Starting %s", config.APP_NAME)

    try:
        from services import build_services
    except ImportError as exc:
        print(
            "A required package is missing.\n"
            "Install the dependencies first:\n\n"
            "    pip install -r requirements.txt\n\n"
            f"({exc})",
            file=sys.stderr,
        )
        return 2

    try:
        services = build_services(use_ai=not args.no_ai)
    except Exception as exc:
        log.exception("Could not start")
        print(f"Could not start the app: {exc}", file=sys.stderr)
        return 2

    try:
        import tkinter  # noqa: F401  (imported for the error message below)
        from ui.app import PlannerApp
    except ImportError as exc:
        print(
            "Tkinter is not available in this Python installation.\n"
            "On Debian/Ubuntu: sudo apt install python3-tk\n"
            f"({exc})\n\n"
            "The command-line version still works:  python cli.py Ibadan football",
            file=sys.stderr,
        )
        return 2

    try:
        app = PlannerApp(services)
        app.mainloop()
    except Exception:
        log.exception("The interface stopped unexpectedly")
        traceback.print_exc()
        return 1

    log.info("Closed cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
