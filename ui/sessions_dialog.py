"""Past-session browser."""
from __future__ import annotations

import time
import tkinter as tk
from tkinter import messagebox
from typing import Callable

import store as S
from ui import widgets as W


class SessionsDialog:
    """Lists stored sessions with duration, alarms and delete/open actions."""

    def __init__(self, parent: tk.Tk, store: S.Store, theme: W.Theme, *,
                 on_open: Callable[[int], None]) -> None:
        self.store = store
        self.theme = theme
        self.on_open = on_open

        self.win = tk.Toplevel(parent)
        self.win.title("gpumon - logged sessions")
        self.win.configure(bg=W.BG)
        self.win.geometry("980x520")
        self.win.transient(parent)

        tk.Label(self.win, text="Logged sessions", bg=W.BG, fg=W.TEXT_BRIGHT,
                 font=theme.title, anchor="w").pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(self.win, text=store.path, bg=W.BG, fg=W.TEXT_DIM,
                 font=theme.small, anchor="w").pack(anchor="w", padx=12)

        body = tk.Frame(self.win, bg=W.BG)
        body.pack(fill="both", expand=True, padx=12, pady=8)

        cols = [("id", 6, "w"), ("started", 20, "w"), ("label", 30, "w"),
                ("duration", 12, "e"), ("samples", 9, "e"), ("alarms", 8, "e"),
                ("devices", 34, "w")]
        self.table = W.StatTable(body, cols, theme=theme)
        self.table.pack(anchor="w", fill="x")

        self.sessions = store.list_sessions(limit=200)
        rows = []
        for s in self.sessions:
            alarms = store.alarms(s.id)
            crit = sum(1 for a in alarms if a.level == "critical")
            devices = ", ".join(f"gpu{d.index} {d.name}" for d in s.devices)
            rows.append([
                str(s.id),
                time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(s.started_at)),
                s.label or "(unlabelled)",
                S.human_duration(s.duration),
                str(s.sample_count),
                f"{len(alarms)}" + (f" ({crit} crit)" if crit else ""),
                devices or "-"])
        self.table.set_rows(rows)
        if not rows:
            tk.Label(body, text="No sessions logged yet.", bg=W.BG, fg=W.TEXT_DIM,
                     font=theme.body).pack(anchor="w", pady=8)

        buttons = tk.Frame(self.win, bg=W.BG)
        buttons.pack(fill="x", padx=12, pady=10)
        W.FlatButton(buttons, " OPEN SELECTED", self.open_selected,
                     bg=W.action_colours("ok")[0], fg=W.OK,
                     theme=theme).pack(side="left")
        W.FlatButton(buttons, " DELETE SELECTED", self.delete_selected,
                     bg=W.action_colours("crit")[0], fg=W.CRIT,
                     theme=theme).pack(side="left", padx=6)
        W.FlatButton(buttons, " CLOSE", self.win.destroy, theme=theme).pack(side="right")

        self.hint = tk.Label(buttons, text="Select a row first.", bg=W.BG,
                             fg=W.TEXT_DIM, font=theme.small)
        self.hint.pack(side="left", padx=12)
        self._selected: int | None = None
        for r, s in enumerate(self.sessions):
            for cell in self.table._rows[r]:
                cell.bind("<Button-1>", lambda _e, sid=s.id: self._select(sid))

    def _select(self, session_id: int) -> None:
        self._selected = session_id
        self.hint.configure(text=f"selected session #{session_id}", fg=W.ACCENT)

    def open_selected(self) -> None:
        if self._selected is None:
            self.hint.configure(text="Select a row first.", fg=W.WARN)
            return
        self.on_open(self._selected)

    def delete_selected(self) -> None:
        if self._selected is None:
            self.hint.configure(text="Select a row first.", fg=W.WARN)
            return
        if not messagebox.askyesno("Delete session",
                                   f"Permanently delete session #{self._selected} "
                                   "and all its samples?", parent=self.win):
            return
        self.store.delete_session(self._selected)
        self.win.destroy()
