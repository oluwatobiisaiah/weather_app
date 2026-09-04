"""
PlannerApp -- the controller.

Its whole job is: collect input, run the slow work on a worker thread, hop the
result back to the UI thread with `after`, and turn any WeatherAppError into a
dialog a person can read.

Threading rule, obeyed everywhere in this file: Tkinter widgets may only be
touched from the thread that owns the mainloop. The worker computes; the
callback draws.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

import config
from core.exceptions import CorruptDataFileError, StorageError, WeatherAppError
from core.models import ActivityPlan, Forecast, Location
from core.validators import is_confident_match, parse_date
from ui.views.result_view import ResultView
from ui.views.search_view import SearchView
from ui.views.sidebar_view import SidebarView
from ui.widgets import ACCENT, INK, INK_2, INK_3, PAPER, LocationChooser, StatusBar

log = logging.getLogger(__name__)


class PlannerApp(tk.Tk):
    def __init__(self, services):
        super().__init__()
        self.services = services
        self.forecast: Forecast | None = None
        self.plan: ActivityPlan | None = None
        self.location: Location | None = None
        self._busy = False

        self.title(config.APP_NAME)
        self.geometry("1180x760")
        self.minsize(980, 640)
        self.configure(background=PAPER)

        self._build_styles()
        self._build_layout()
        self._refresh_sidebar()

        self.status.set("Ready. Enter a location and choose an activity.")
        self.status.set_source(
            "AI narration: on" if services.ai_enabled else "AI narration: off (rules only)"
        )
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    # -----------------------------------------------------------------------
    # Chrome
    # -----------------------------------------------------------------------
    def _build_styles(self) -> None:
        style = ttk.Style(self)
        # 'clam' honours colour settings on every platform; the default Windows
        # theme silently ignores half of them.
        if "clam" in style.theme_names():
            style.theme_use("clam")

        style.configure(".", background=PAPER, foreground=INK, font=("Segoe UI", 10))
        style.configure("TFrame", background=PAPER)
        style.configure("Bar.TFrame", background="#ffffff")
        style.configure("TLabel", background=PAPER, foreground=INK)
        style.configure("Field.TLabel", background="#ffffff", foreground=INK_2)
        style.configure(
            "Title.TLabel", background="#ffffff", foreground=INK, font=("Segoe UI Semibold", 13)
        )
        style.configure("Sub.TLabel", background="#ffffff", foreground=INK_3)
        style.configure("Headline.TLabel", background="#ffffff", foreground=INK_2)
        style.configure(
            "Section.TLabel", foreground=INK_3, font=("Consolas", 8, "bold"), background=PAPER
        )
        style.configure("Hint.TLabel", foreground=INK_3, background=PAPER)
        style.configure("Flag.TLabel", foreground="#9c1f28", background=PAPER)
        style.configure("Meter.TLabel", background="#ffffff", foreground=INK_2)
        style.configure(
            "MeterValue.TLabel", background="#ffffff", foreground=INK, font=("Consolas", 9)
        )
        style.configure("Status.TLabel", background="#ffffff", foreground=INK_2)
        style.configure("StatusRight.TLabel", background="#ffffff", foreground=INK_3)
        style.configure("Accent.TButton", foreground="#ffffff", background=ACCENT)
        style.map(
            "Accent.TButton",
            background=[("active", "#0a5a63"), ("disabled", "#9db6ba")],
        )
        style.configure("Treeview", background="#ffffff", fieldbackground="#ffffff", rowheight=22)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        style.configure("TNotebook", background=PAPER, borderwidth=0)
        style.configure("TNotebook.Tab", padding=(14, 7))

    def _build_layout(self) -> None:
        self.search = SearchView(
            self,
            activities=self.services.profiles,
            on_check=self.on_check,
            on_favourite=self.on_toggle_favourite,
        )
        self.search.pack(fill="x")
        ttk.Separator(self, orient="horizontal").pack(fill="x")

        body = ttk.Frame(self, padding=(10, 10))
        body.pack(fill="both", expand=True)

        self.sidebar = SidebarView(
            body,
            on_favourite_open=self.on_open_favourite,
            on_history_open=self.on_open_history,
            on_plan_open=self.on_open_plan,
            on_favourite_remove=self.on_remove_favourite,
        )
        self.sidebar.pack(side="left", fill="y", padx=(0, 10))
        self.sidebar.configure(width=230)
        self.sidebar.pack_propagate(False)

        self.results = ResultView(
            body,
            analyzer=self.services.analyzer,
            on_save_plan=self.on_save_plan,
            on_export_plan=self.on_export_plan,
        )
        self.results.pack(side="left", fill="both", expand=True)

        ttk.Separator(self, orient="horizontal").pack(fill="x")
        self.status = StatusBar(self, style="Bar.TFrame")
        self.status.pack(fill="x")

    # -----------------------------------------------------------------------
    # The search
    # -----------------------------------------------------------------------
    def on_check(self) -> None:
        """Validate on the UI thread, then hand the slow part to a worker."""
        if self._busy:
            return

        query = self.search.location
        activity = self.search.activity_key

        try:
            today = date.today()
            day = parse_date(
                self.search.date_text,
                earliest=today,
                latest=today + timedelta(days=config.FORECAST_DAYS - 1),
            )
        except WeatherAppError as exc:
            self._show_error(exc)
            return

        self._set_busy(True)
        self.status.set(f"Looking up {query.strip() or 'that location'}…")

        def work() -> None:
            try:
                candidates = self.services.client.geocode(query)
                self._post(self._location_resolved, candidates, query, day, activity)
            except WeatherAppError as exc:
                self._post(self._show_error, exc)
            except Exception as exc:  # never let a worker die silently
                log.exception("Unexpected failure during geocoding")
                self._post(self._show_unexpected, exc)

        threading.Thread(target=work, daemon=True).start()

    def _location_resolved(self, candidates, query: str, day: date, activity: str) -> None:
        """
        Ask before assuming. The geocoder happily matches near-misses, so a
        typed 'Lagoss' comes back as Lagossa in Tanzania -- a real place, and
        the wrong one. Only an unambiguous match skips the question.
        """
        location = candidates[0]
        # Note this is NOT gated on len(candidates) > 1: "Lagoss" returns the
        # single result "Lagossa, Tanzania", and one confident wrong answer is
        # more dangerous than an ambiguous list.
        if not is_confident_match(query, location.name):
            chooser = LocationChooser(self, candidates, query.strip())
            if chooser.result is None:
                self._set_busy(False)
                self.status.set("Search cancelled.")
                return
            location = chooser.result

        self.status.set(f"Fetching the forecast for {location.name}…")

        def work() -> None:
            try:
                forecast = self.services.client.fetch_forecast(location)
                plan = self.services.engine.build_plan(forecast, day, activity)
                try:
                    self.services.storage.record_search(plan)
                except StorageError as exc:
                    log.warning("Could not record the search: %s", exc)
                self._post(self._show_plan, plan, forecast, location)
            except WeatherAppError as exc:
                self._post(self._show_error, exc)
            except Exception as exc:
                log.exception("Unexpected failure during forecast")
                self._post(self._show_unexpected, exc)

        threading.Thread(target=work, daemon=True).start()

    # -----------------------------------------------------------------------
    # Callbacks that run on the UI thread
    # -----------------------------------------------------------------------
    def _show_plan(self, plan: ActivityPlan, forecast: Forecast, location: Location) -> None:
        self.plan = plan
        self.forecast = forecast
        self.location = location

        self.results.show_plan(plan, forecast)
        self.search.location = location.display_name
        self.search.set_favourite_state(
            enabled=True, already=self._is_favourite_safe(location)
        )
        self._set_busy(False)
        self._refresh_sidebar()

        age = f"fetched {forecast.fetched_at:%H:%M}"
        if forecast.from_cache:
            age = f"cached forecast from {forecast.fetched_at:%H:%M} ({forecast.age_minutes} min old)"
        self.status.set(f"{plan.band.value} · {plan.assessment.score:.0f}/100 · {age}")
        self.status.set_source(
            "Wording: Gemini" if plan.ai_used else "Wording: built-in rules"
        )

    def _show_error(self, exc: WeatherAppError) -> None:
        self._set_busy(False)
        self.status.set(exc.user_message)
        messagebox.showwarning(
            title=type(exc).__name__.replace("Error", "").strip() or "Problem",
            message=exc.user_message,
            detail=exc.hint,
            parent=self,
        )
        if isinstance(exc, CorruptDataFileError):
            self._refresh_sidebar()

    def _show_unexpected(self, exc: Exception) -> None:
        self._set_busy(False)
        self.status.set("Something went wrong; see logs/app.log.")
        messagebox.showerror(
            title="Unexpected problem",
            message="Something went wrong that the app did not expect.",
            detail=f"{type(exc).__name__}: {exc}\n\nThe details are in logs/app.log.",
            parent=self,
        )

    # -----------------------------------------------------------------------
    # Favourites, history, saved plans
    # -----------------------------------------------------------------------
    def on_toggle_favourite(self) -> None:
        if not self.location:
            return
        try:
            if self._is_favourite_safe(self.location):
                self.services.storage.remove_favourite(self.location.key())
                self.status.set(f"Removed {self.location.name} from favourites.")
            else:
                self.services.storage.add_favourite(self.location)
                self.status.set(f"Saved {self.location.name} to favourites.")
            self.search.set_favourite_state(
                enabled=True, already=self._is_favourite_safe(self.location)
            )
            self._refresh_sidebar()
        except WeatherAppError as exc:
            self._show_error(exc)

    def on_remove_favourite(self, entry: dict) -> None:
        try:
            self.services.storage.remove_favourite(entry.get("key", ""))
            self.status.set(f"Removed {entry.get('name', 'that location')}.")
            self._refresh_sidebar()
        except WeatherAppError as exc:
            self._show_error(exc)

    def on_open_favourite(self, entry: dict) -> None:
        self.search.location = entry.get("display_name") or entry.get("name", "")
        self.on_check()

    def on_open_history(self, entry: dict) -> None:
        self.search.location = entry.get("display_name") or entry.get("location", {}).get("name", "")
        self.search.set_activity_key(entry.get("activity_key", ""))
        day = entry.get("day", "")
        if day >= date.today().isoformat():
            self.search.set_date(day)
        self.on_check()

    def on_open_plan(self, entry: dict) -> None:
        try:
            plan = self.services.storage.load_plan(entry["file"])
        except WeatherAppError as exc:
            self._show_error(exc)
            return
        self.plan = plan
        self.forecast = None
        self.location = plan.location
        self.results.show_plan(plan, None)
        self.search.location = plan.location.display_name
        self.search.set_activity_key(plan.activity_key)
        self.search.set_favourite_state(enabled=True, already=self._is_favourite_safe(plan.location))
        self.status.set(f"Opened saved plan from {plan.created_at:%d %b %H:%M}.")

    def on_save_plan(self) -> None:
        if not self.plan:
            return
        self.plan.checklist = self.results.collect_checklist()
        try:
            path = self.services.storage.save_plan(self.plan)
        except WeatherAppError as exc:
            self._show_error(exc)
            return
        self.status.set(f"Saved to {path.name} (and {path.stem}.txt).")
        self._refresh_sidebar()

    def on_export_plan(self) -> None:
        if not self.plan:
            return
        self.plan.checklist = self.results.collect_checklist()
        suggested = f"{self.services.storage.plan_filename(self.plan)}.txt"
        target = filedialog.asksaveasfilename(
            parent=self,
            title="Export plan",
            defaultextension=".txt",
            initialfile=suggested,
            filetypes=[("Text file", "*.txt"), ("All files", "*.*")],
        )
        if not target:
            return
        try:
            self.services.storage.export_plan_text(self.plan, Path(target))
        except WeatherAppError as exc:
            self._show_error(exc)
            return
        self.status.set(f"Exported to {Path(target).name}.")

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------
    def _refresh_sidebar(self) -> None:
        """Re-read the three lists, surviving a damaged file on the way."""
        try:
            self.sidebar.set_favourites(self.services.storage.list_favourites())
        except WeatherAppError as exc:
            self.sidebar.set_favourites([])
            self._show_error(exc)
        try:
            self.sidebar.set_history(self.services.storage.recent_searches())
        except WeatherAppError as exc:
            self.sidebar.set_history([])
            self._show_error(exc)
        try:
            self.sidebar.set_plans(self.services.storage.list_plans())
        except WeatherAppError:
            self.sidebar.set_plans([])

    def _is_favourite_safe(self, location: Location) -> bool:
        try:
            return self.services.storage.is_favourite(location)
        except WeatherAppError:
            return False

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.search.set_busy(busy)
        self.configure(cursor="watch" if busy else "")

    def _post(self, func, *args) -> None:
        """Hop back to the UI thread. The only safe way to draw from a worker."""
        self.after(0, func, *args)
