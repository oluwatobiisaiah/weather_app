"""
The left rail: favourite locations, recent searches, and saved plans.

Everything here is read back from disk, so it is also the visible proof that
the file-handling layer works: close the app, reopen it, the lists are intact.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ui.widgets import INK_3


class SidebarView(ttk.Frame):
    def __init__(self, master, *, on_favourite_open, on_history_open, on_plan_open,
                 on_favourite_remove, **kwargs):
        super().__init__(master, **kwargs)
        self.on_favourite_open = on_favourite_open
        self.on_history_open = on_history_open
        self.on_plan_open = on_plan_open
        self.on_favourite_remove = on_favourite_remove

        self._favourites: list[dict] = []
        self._history: list[dict] = []
        self._plans: list[dict] = []

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        # -- favourites -----------------------------------------------------
        fav_frame = ttk.Frame(notebook, padding=6)
        self.fav_list = tk.Listbox(
            fav_frame, activestyle="none", borderwidth=1, relief="solid",
            highlightthickness=0, exportselection=False,
        )
        self.fav_list.pack(fill="both", expand=True)
        self.fav_list.bind("<Double-Button-1>", self._open_favourite)
        self.fav_list.bind("<Return>", self._open_favourite)

        buttons = ttk.Frame(fav_frame)
        buttons.pack(fill="x", pady=(6, 0))
        ttk.Button(buttons, text="Open", command=self._open_favourite).pack(side="left")
        ttk.Button(buttons, text="Remove", command=self._remove_favourite).pack(
            side="left", padx=(6, 0)
        )
        notebook.add(fav_frame, text="Favourites")

        # -- history --------------------------------------------------------
        hist_frame = ttk.Frame(notebook, padding=6)
        self.hist_list = tk.Listbox(
            hist_frame, activestyle="none", borderwidth=1, relief="solid",
            highlightthickness=0, exportselection=False,
        )
        self.hist_list.pack(fill="both", expand=True)
        self.hist_list.bind("<Double-Button-1>", self._open_history)
        self.hist_list.bind("<Return>", self._open_history)
        ttk.Label(
            hist_frame, text="Double-click to run it again", foreground=INK_3
        ).pack(anchor="w", pady=(6, 0))
        notebook.add(hist_frame, text="Recent")

        # -- saved plans ----------------------------------------------------
        plan_frame = ttk.Frame(notebook, padding=6)
        self.plan_list = tk.Listbox(
            plan_frame, activestyle="none", borderwidth=1, relief="solid",
            highlightthickness=0, exportselection=False,
        )
        self.plan_list.pack(fill="both", expand=True)
        self.plan_list.bind("<Double-Button-1>", self._open_plan)
        self.plan_list.bind("<Return>", self._open_plan)
        ttk.Label(plan_frame, text="Saved to data/plans/", foreground=INK_3).pack(
            anchor="w", pady=(6, 0)
        )
        notebook.add(plan_frame, text="Plans")

    # -----------------------------------------------------------------------
    def set_favourites(self, favourites: list[dict]) -> None:
        self._favourites = favourites
        self.fav_list.delete(0, tk.END)
        for entry in favourites:
            self.fav_list.insert(tk.END, f"★ {entry.get('display_name', entry.get('name', '?'))}")
        if not favourites:
            self.fav_list.insert(tk.END, "  (none yet)")

    def set_history(self, history: list[dict]) -> None:
        self._history = history
        self.hist_list.delete(0, tk.END)
        for entry in history:
            self.hist_list.insert(
                tk.END,
                f"{entry.get('location', {}).get('name', '?')} · "
                f"{entry.get('activity_label', '?')} · {entry.get('band', '')}",
            )
        if not history:
            self.hist_list.insert(tk.END, "  (no searches yet)")

    def set_plans(self, plans: list[dict]) -> None:
        self._plans = plans
        self.plan_list.delete(0, tk.END)
        for entry in plans:
            self.plan_list.insert(tk.END, entry.get("label", entry.get("file", "?")))
        if not plans:
            self.plan_list.insert(tk.END, "  (no saved plans)")

    # -----------------------------------------------------------------------
    def _selected(self, listbox: tk.Listbox, source: list[dict]) -> dict | None:
        selection = listbox.curselection()
        if not selection or selection[0] >= len(source):
            return None
        return source[selection[0]]

    def _open_favourite(self, _event=None) -> None:
        entry = self._selected(self.fav_list, self._favourites)
        if entry:
            self.on_favourite_open(entry)

    def _remove_favourite(self) -> None:
        entry = self._selected(self.fav_list, self._favourites)
        if entry:
            self.on_favourite_remove(entry)

    def _open_history(self, _event=None) -> None:
        entry = self._selected(self.hist_list, self._history)
        if entry:
            self.on_history_open(entry)

    def _open_plan(self, _event=None) -> None:
        entry = self._selected(self.plan_list, self._plans)
        if entry:
            self.on_plan_open(entry)
