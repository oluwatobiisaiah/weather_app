"""
Reusable widgets: the verdict badge, the factor meter, a scrollable frame, the
packing checklist and the status bar.

They are plain Tkinter -- no images, no third-party themes -- so the app starts
on a bare Python install.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from core.models import FACTOR_LABELS, ChecklistItem, RiskBand

# A small palette shared by every custom widget.
PAPER = "#eceff0"
SURFACE = "#ffffff"
INK = "#0e181c"
INK_2 = "#3c4f56"
INK_3 = "#69808a"
RULE = "#ccd8da"
ACCENT = "#0c6b76"

BAND_COLOURS = {
    RiskBand.SAFE: "#1b7a4e",
    RiskBand.MANAGEABLE: "#8a6810",
    RiskBand.RISKY: "#b0521e",
    RiskBand.AVOID: "#9c1f28",
}


def colour_for_score(score: float) -> str:
    """Semantic colour for any 0-100 penalty, using the same four bands."""
    return BAND_COLOURS[RiskBand.from_score(score)]


class ScrollableFrame(ttk.Frame):
    """A frame that scrolls vertically. Put content in `.body`."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0, background=SURFACE)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.body = ttk.Frame(self.canvas, padding=(14, 12))

        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)

        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")

        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda _e: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda _e: self._unbind_wheel())

    def _on_body_configure(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event) -> None:
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self) -> None:
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)

    def _unbind_wheel(self) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def clear(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()


class VerdictBadge(tk.Canvas):
    """The big coloured word: Safe / Manageable / Risky / Avoid."""

    def __init__(self, master, width: int = 196, height: int = 78):
        super().__init__(
            master, width=width, height=height, highlightthickness=0, background=PAPER
        )
        self._width = width
        self._height = height
        self.set(None, 0.0)

    def set(self, band: RiskBand | None, score: float) -> None:
        self.delete("all")
        if band is None:
            self.create_rectangle(
                0, 0, self._width, self._height, fill=PAPER, outline=RULE, dash=(4, 3)
            )
            self.create_text(
                self._width / 2,
                self._height / 2,
                text="No result yet",
                fill=INK_3,
                font=("Segoe UI", 10),
            )
            return

        colour = BAND_COLOURS[band]
        self.create_rectangle(0, 0, self._width, self._height, fill=colour, outline=colour)
        self.create_text(
            self._width / 2,
            self._height / 2 - 11,
            text=band.value.upper(),
            fill="#ffffff",
            font=("Segoe UI Semibold", 17),
        )
        self.create_text(
            self._width / 2,
            self._height / 2 + 16,
            text=f"{score:.0f} / 100 risk",
            fill="#ffffff",
            font=("Consolas", 10),
        )


class RiskMeter(ttk.Frame):
    """One labelled bar per weather factor, worst first."""

    BAR_WIDTH = 190
    BAR_HEIGHT = 12

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._rows: list[tuple[ttk.Label, tk.Canvas, ttk.Label]] = []

    def set(self, factors: dict[str, float]) -> None:
        for label, canvas, value in self._rows:
            label.destroy()
            canvas.destroy()
            value.destroy()
        self._rows.clear()

        ordered = sorted(factors.items(), key=lambda kv: kv[1], reverse=True)
        for row, (name, score) in enumerate(ordered):
            label = ttk.Label(
                self, text=FACTOR_LABELS.get(name, name.title()), width=11, style="Meter.TLabel"
            )
            canvas = tk.Canvas(
                self,
                width=self.BAR_WIDTH,
                height=self.BAR_HEIGHT,
                highlightthickness=0,
                background=SURFACE,
            )
            canvas.create_rectangle(
                0, 0, self.BAR_WIDTH, self.BAR_HEIGHT, fill="#e4eaeb", outline="#e4eaeb"
            )
            filled = max(2, int(self.BAR_WIDTH * min(100.0, max(0.0, score)) / 100))
            colour = colour_for_score(score)
            canvas.create_rectangle(0, 0, filled, self.BAR_HEIGHT, fill=colour, outline=colour)
            value = ttk.Label(self, text=f"{score:>3.0f}", width=4, style="MeterValue.TLabel")

            label.grid(row=row, column=0, sticky="w", pady=2)
            canvas.grid(row=row, column=1, padx=(8, 8), pady=2)
            value.grid(row=row, column=2, sticky="w", pady=2)
            self._rows.append((label, canvas, value))


class ChecklistFrame(ScrollableFrame):
    """The packing list: a checkbutton per item, ticks kept with the plan."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._entries: list[tuple[ChecklistItem, tk.BooleanVar]] = []

    def set_items(self, items: list[ChecklistItem]) -> None:
        self.clear()
        self._entries.clear()

        if not items:
            ttk.Label(self.body, text="Nothing to pack yet; run a search.").pack(anchor="w")
            return

        for item in items:
            var = tk.BooleanVar(value=item.checked)
            row = ttk.Frame(self.body)
            row.pack(fill="x", anchor="w", pady=1)

            text = f"{item.item}  ★" if item.essential else item.item
            ttk.Checkbutton(row, text=text, variable=var).pack(anchor="w")
            if item.reason:
                ttk.Label(
                    row, text=f"     {item.reason}", style="Hint.TLabel", wraplength=430
                ).pack(anchor="w")
            self._entries.append((item, var))

    def collect(self) -> list[ChecklistItem]:
        """Read the ticks back so they are saved with the plan."""
        for item, var in self._entries:
            item.checked = bool(var.get())
        return [item for item, _ in self._entries]


class LocationChooser(tk.Toplevel):
    """
    Ask which place was meant when the search was ambiguous.

    Typing "Lagoss" returns Lagossa in Tanzania -- a real place, so it is not an
    error, but almost certainly not what was wanted. Silently taking the first
    result would give a confident forecast for the wrong continent.
    """

    def __init__(self, parent, candidates, query: str):
        super().__init__(parent)
        self.result = None
        self._candidates = candidates

        self.title("Did you mean…?")
        self.transient(parent)
        self.resizable(False, False)
        self.configure(background=PAPER)

        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)

        if len(candidates) > 1:
            heading = f"More than one place matches “{query}”."
            sub = "Pick the one you meant:"
        else:
            heading = f"No exact match for “{query}”."
            sub = "The closest place found was:"

        ttk.Label(frame, text=heading, font=("Segoe UI Semibold", 10)).pack(anchor="w")
        ttk.Label(frame, text=sub, foreground=INK_3).pack(anchor="w", pady=(2, 10))

        self.listbox = tk.Listbox(
            frame, height=min(6, len(candidates)), width=46, activestyle="none",
            borderwidth=1, relief="solid", highlightthickness=0, exportselection=False,
        )
        for candidate in candidates:
            self.listbox.insert(tk.END, f"  {candidate.display_name}")
        self.listbox.selection_set(0)
        self.listbox.pack(fill="x")
        self.listbox.bind("<Double-Button-1>", lambda _e: self._accept())
        self.listbox.bind("<Return>", lambda _e: self._accept())

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(12, 0))
        ttk.Button(buttons, text="Use this one", command=self._accept).pack(side="right")
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(
            side="right", padx=(0, 8)
        )

        self.bind("<Escape>", lambda _e: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self.listbox.focus_set()
        self.grab_set()          # modal: the question must be answered first
        parent.wait_window(self)

    def _accept(self) -> None:
        selection = self.listbox.curselection()
        if selection:
            self.result = self._candidates[selection[0]]
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


class StatusBar(ttk.Frame):
    """One line at the bottom that always says what just happened."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._text = tk.StringVar(value="Ready.")
        self._source = tk.StringVar(value="")

        ttk.Label(self, textvariable=self._text, style="Status.TLabel").pack(
            side="left", padx=(12, 8), pady=5
        )
        ttk.Label(self, textvariable=self._source, style="StatusRight.TLabel").pack(
            side="right", padx=(8, 12), pady=5
        )

    def set(self, message: str) -> None:
        self._text.set(message)

    def set_source(self, message: str) -> None:
        self._source.set(message)
