"""
The search bar: location, activity, date, and the two buttons.

The date field is an *editable* combobox on purpose. Picking from the list is
the easy path, but typing into it runs the same `parse_date` regex and bounds
check the CLI uses, so bad input is caught here rather than at the API.
"""

from __future__ import annotations

import tkinter as tk
from datetime import date, timedelta
from tkinter import ttk


class SearchView(ttk.Frame):
    def __init__(self, master, *, activities: dict, on_check, on_favourite, **kwargs):
        super().__init__(master, style="Bar.TFrame", padding=(14, 12), **kwargs)
        self.on_check = on_check
        self.on_favourite = on_favourite

        # key <-> label maps, so the combobox shows "Outdoor event" but the
        # engine receives "outdoor_event".
        self._label_to_key = {p.label: key for key, p in activities.items()}
        labels = list(self._label_to_key)

        self.location_var = tk.StringVar()
        self.activity_var = tk.StringVar(value=labels[0] if labels else "")
        self.date_var = tk.StringVar()

        ttk.Label(self, text="Location", style="Field.TLabel").grid(
            row=0, column=0, sticky="w", padx=(0, 6)
        )
        self.location_entry = ttk.Entry(self, textvariable=self.location_var, width=26)
        self.location_entry.grid(row=0, column=1, sticky="w", padx=(0, 16))
        self.location_entry.bind("<Return>", lambda _e: self.on_check())

        ttk.Label(self, text="Activity", style="Field.TLabel").grid(
            row=0, column=2, sticky="w", padx=(0, 6)
        )
        self.activity_box = ttk.Combobox(
            self, textvariable=self.activity_var, values=labels, state="readonly", width=16
        )
        self.activity_box.grid(row=0, column=3, sticky="w", padx=(0, 16))

        ttk.Label(self, text="Date", style="Field.TLabel").grid(
            row=0, column=4, sticky="w", padx=(0, 6)
        )
        self.date_box = ttk.Combobox(self, textvariable=self.date_var, width=13)
        self.date_box.grid(row=0, column=5, sticky="w", padx=(0, 16))
        self._load_dates()

        self.check_button = ttk.Button(
            self, text="Check conditions", command=self.on_check, style="Accent.TButton"
        )
        self.check_button.grid(row=0, column=6, sticky="w", padx=(0, 8))

        self.favourite_button = ttk.Button(
            self, text="★ Save location", command=self.on_favourite, state="disabled"
        )
        self.favourite_button.grid(row=0, column=7, sticky="w")

        self.columnconfigure(8, weight=1)
        self.location_entry.focus_set()

    # -----------------------------------------------------------------------
    def _load_dates(self, days: int = 7) -> None:
        today = date.today()
        values = [(today + timedelta(days=i)).isoformat() for i in range(days)]
        self.date_box["values"] = values
        self.date_var.set(values[0])

    # -----------------------------------------------------------------------
    @property
    def location(self) -> str:
        return self.location_var.get()

    @location.setter
    def location(self, value: str) -> None:
        self.location_var.set(value)

    @property
    def activity_key(self) -> str:
        return self._label_to_key.get(self.activity_var.get(), "")

    def set_activity_key(self, key: str) -> None:
        for label, mapped in self._label_to_key.items():
            if mapped == key:
                self.activity_var.set(label)
                return

    @property
    def date_text(self) -> str:
        return self.date_var.get()

    def set_date(self, value: str) -> None:
        self.date_var.set(value)

    # -----------------------------------------------------------------------
    def set_busy(self, busy: bool) -> None:
        """Disable the trigger while a fetch is in flight."""
        self.check_button.state(["disabled"] if busy else ["!disabled"])
        self.check_button.configure(text="Checking…" if busy else "Check conditions")

    def set_favourite_state(self, *, enabled: bool, already: bool) -> None:
        self.favourite_button.state(["!disabled"] if enabled else ["disabled"])
        self.favourite_button.configure(
            text="★ Saved" if already else "★ Save location"
        )
