"""
The results panel: verdict header plus four tabs.

  Overview  -- the verdict, the best windows, the factor breakdown
  Timeline  -- every forecast hour, colour-coded by risk
  Advice    -- the written explanation and the safety points
  Packing   -- the checklist, with Save and Export

The view is passive: it renders an ActivityPlan and reports button presses back
to the controller. It never fetches or scores anything itself.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from core.models import ActivityPlan, Forecast, RiskBand
from ui.widgets import (
    BAND_COLOURS,
    INK_3,
    ChecklistFrame,
    RiskMeter,
    VerdictBadge,
)


class ResultView(ttk.Frame):
    def __init__(self, master, *, analyzer, on_save_plan, on_export_plan, **kwargs):
        super().__init__(master, **kwargs)
        self.analyzer = analyzer
        self.on_save_plan = on_save_plan
        self.on_export_plan = on_export_plan
        self._plan: ActivityPlan | None = None

        # -- header ---------------------------------------------------------
        header = ttk.Frame(self, style="Bar.TFrame", padding=(14, 12))
        header.pack(fill="x")

        self.badge = VerdictBadge(header)
        self.badge.pack(side="left")

        titles = ttk.Frame(header, style="Bar.TFrame")
        titles.pack(side="left", padx=(16, 0), fill="x", expand=True)

        self.title_var = tk.StringVar(value="Enter a location and press Check conditions")
        self.subtitle_var = tk.StringVar(value="")
        self.headline_var = tk.StringVar(value="")

        ttk.Label(titles, textvariable=self.title_var, style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, textvariable=self.subtitle_var, style="Sub.TLabel").pack(
            anchor="w", pady=(2, 0)
        )
        ttk.Label(
            titles, textvariable=self.headline_var, style="Headline.TLabel", wraplength=560
        ).pack(anchor="w", pady=(6, 0))

        # -- tabs -----------------------------------------------------------
        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=(10, 10))

        self._build_overview()
        self._build_timeline()
        self._build_advice()
        self._build_packing()

    # -----------------------------------------------------------------------
    # Tab construction
    # -----------------------------------------------------------------------
    def _build_overview(self) -> None:
        tab = ttk.Frame(self.notebook, padding=14)
        self.notebook.add(tab, text="Overview")

        ttk.Label(tab, text="BEST TIMES", style="Section.TLabel").pack(anchor="w")
        self.windows_tree = ttk.Treeview(
            tab, columns=("slot", "band", "score"), show="headings", height=3
        )
        self.windows_tree.heading("slot", text="Time window")
        self.windows_tree.heading("band", text="Verdict")
        self.windows_tree.heading("score", text="Risk")
        self.windows_tree.column("slot", width=150, anchor="w")
        self.windows_tree.column("band", width=120, anchor="w")
        self.windows_tree.column("score", width=70, anchor="e")
        self.windows_tree.pack(fill="x", pady=(6, 4))
        _tag_bands(self.windows_tree)

        self.timing_var = tk.StringVar(value="")
        ttk.Label(tab, textvariable=self.timing_var, style="Hint.TLabel", wraplength=560).pack(
            anchor="w", pady=(0, 12)
        )

        ttk.Label(tab, text="CONDITION BREAKDOWN", style="Section.TLabel").pack(anchor="w")
        self.meter = RiskMeter(tab)
        self.meter.pack(anchor="w", pady=(6, 12))

        self.flags_frame = ttk.Frame(tab)
        self.flags_frame.pack(fill="x", anchor="w")

    def _build_timeline(self) -> None:
        tab = ttk.Frame(self.notebook, padding=(14, 12))
        self.notebook.add(tab, text="Timeline")

        columns = ("time", "temp", "feels", "rain", "wind", "uv", "sky", "risk")
        self.timeline = ttk.Treeview(tab, columns=columns, show="headings", height=16)
        headings = {
            "time": ("Time", 60),
            "temp": ("Temp", 60),
            "feels": ("Feels", 60),
            "rain": ("Rain", 60),
            "wind": ("Wind", 80),
            "uv": ("UV", 50),
            "sky": ("Conditions", 190),
            "risk": ("Risk", 110),
        }
        for key, (title, width) in headings.items():
            self.timeline.heading(key, text=title)
            anchor = "w" if key == "sky" else ("w" if key == "time" else "e")
            self.timeline.column(key, width=width, anchor=anchor, stretch=(key == "sky"))

        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.timeline.yview)
        self.timeline.configure(yscrollcommand=scroll.set)
        self.timeline.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        _tag_bands(self.timeline)

    def _build_advice(self) -> None:
        tab = ttk.Frame(self.notebook, padding=(14, 12))
        self.notebook.add(tab, text="Advice")

        self.advice_text = tk.Text(
            tab, wrap="word", height=16, relief="flat", padx=10, pady=10,
            background="#ffffff", foreground="#0e181c", font=("Segoe UI", 10),
        )
        scroll = ttk.Scrollbar(tab, orient="vertical", command=self.advice_text.yview)
        self.advice_text.configure(yscrollcommand=scroll.set, state="disabled")
        self.advice_text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self.advice_text.tag_configure("h", font=("Segoe UI Semibold", 10), spacing1=8, spacing3=4)
        self.advice_text.tag_configure("body", spacing3=8)
        self.advice_text.tag_configure("bullet", lmargin1=14, lmargin2=28, spacing3=4)
        self.advice_text.tag_configure("flag", foreground="#9c1f28", lmargin1=14, lmargin2=28)
        self.advice_text.tag_configure("note", foreground=INK_3, spacing1=10)

    def _build_packing(self) -> None:
        tab = ttk.Frame(self.notebook, padding=(0, 0))
        self.notebook.add(tab, text="Packing")

        self.checklist = ChecklistFrame(tab)
        self.checklist.pack(fill="both", expand=True)

        buttons = ttk.Frame(tab, padding=(14, 10))
        buttons.pack(fill="x")
        self.save_button = ttk.Button(
            buttons, text="Save plan", command=self.on_save_plan, state="disabled"
        )
        self.save_button.pack(side="left")
        self.export_button = ttk.Button(
            buttons, text="Export as text…", command=self.on_export_plan, state="disabled"
        )
        self.export_button.pack(side="left", padx=(8, 0))
        ttk.Label(buttons, text="★ = essential", style="Hint.TLabel").pack(side="right")

    # -----------------------------------------------------------------------
    # Rendering
    # -----------------------------------------------------------------------
    def show_plan(self, plan: ActivityPlan, forecast: Forecast | None = None) -> None:
        self._plan = plan

        self.badge.set(plan.band, plan.assessment.score)
        self.title_var.set(f"{plan.activity_label} · {plan.location.display_name}")
        self.subtitle_var.set(plan.day.strftime("%A %d %B %Y"))
        self.headline_var.set(plan.headline or plan.band.verdict_sentence)

        # -- best windows ---------------------------------------------------
        self.windows_tree.delete(*self.windows_tree.get_children())
        for i, window in enumerate(plan.windows):
            band = window.assessment.band
            label = f"{'BEST  ' if i == 0 else '      '}{window.label}"
            self.windows_tree.insert(
                "",
                "end",
                values=(label, band.value, f"{window.assessment.score:.0f}"),
                tags=(band.name.lower(),),
            )
        self.timing_var.set(plan.timing_note)

        # -- factors --------------------------------------------------------
        self.meter.set(plan.assessment.factors)

        for child in self.flags_frame.winfo_children():
            child.destroy()
        if plan.assessment.reasons:
            ttk.Label(self.flags_frame, text="SAFETY FLAGS", style="Section.TLabel").pack(
                anchor="w", pady=(0, 4)
            )
            for reason in plan.assessment.reasons:
                ttk.Label(
                    self.flags_frame, text=f"!  {reason}", style="Flag.TLabel", wraplength=560
                ).pack(anchor="w")

        # -- timeline -------------------------------------------------------
        self.timeline.delete(*self.timeline.get_children())
        if forecast is not None:
            self._fill_timeline(plan, forecast)

        # -- advice ---------------------------------------------------------
        self._fill_advice(plan)

        # -- packing --------------------------------------------------------
        self.checklist.set_items(plan.checklist)
        self.save_button.state(["!disabled"])
        self.export_button.state(["!disabled"])

    def _fill_timeline(self, plan: ActivityPlan, forecast: Forecast) -> None:
        profile = self.analyzer.profiles.get(plan.activity_key)
        if profile is None:
            return

        for hour in forecast.hours_for_date(plan.day):
            assessment = self.analyzer.assess_hour(hour, profile)
            band = assessment.band
            self.timeline.insert(
                "",
                "end",
                values=(
                    hour.label,
                    _fmt(hour.temperature_c, "°C"),
                    _fmt(hour.feels_like, "°C"),
                    _fmt(hour.precip_prob, "%", 0),
                    _fmt(hour.wind_kmh, " km/h", 0),
                    _fmt(hour.uv, "", 1),
                    hour.description + ("" if hour.is_daylight else "  (night)"),
                    f"{band.value} {assessment.score:.0f}",
                ),
                tags=(band.name.lower(),),
            )

    def _fill_advice(self, plan: ActivityPlan) -> None:
        self.advice_text.configure(state="normal")
        self.advice_text.delete("1.0", tk.END)

        self.advice_text.insert(tk.END, f"{plan.headline}\n", "h")
        if plan.explanation:
            self.advice_text.insert(tk.END, f"{plan.explanation}\n", "body")

        if plan.assessment.reasons:
            self.advice_text.insert(tk.END, "Safety flags\n", "h")
            for reason in plan.assessment.reasons:
                self.advice_text.insert(tk.END, f"!  {reason}\n", "flag")

        if plan.advice:
            self.advice_text.insert(tk.END, "What to do\n", "h")
            for item in plan.advice:
                self.advice_text.insert(tk.END, f"•  {item}\n", "bullet")

        source = "Wording by Gemini." if plan.ai_used else "Wording by the built-in rules."
        note = f"\n{source}"
        if plan.ai_note:
            note += f"  {plan.ai_note}"
        self.advice_text.insert(tk.END, note, "note")
        self.advice_text.configure(state="disabled")

    # -----------------------------------------------------------------------
    def collect_checklist(self):
        return self.checklist.collect()

    def reset(self) -> None:
        self.badge.set(None, 0)
        self.title_var.set("Enter a location and press Check conditions")
        self.subtitle_var.set("")
        self.headline_var.set("")


def _tag_bands(tree: ttk.Treeview) -> None:
    for band in RiskBand:
        tree.tag_configure(band.name.lower(), foreground=BAND_COLOURS[band])


def _fmt(value, suffix: str = "", places: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{places}f}{suffix}"
