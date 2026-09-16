"""Alarm threshold editor."""
from __future__ import annotations

import tkinter as tk
from typing import Callable, Sequence

import alarms as A
import metrics as M
from ui import widgets as W


class AlarmConfigDialog:
    """Per-metric warning/critical thresholds with hysteresis and debounce."""

    def __init__(self, parent: tk.Tk, engine: A.AlarmEngine, theme: W.Theme, *,
                 metrics: Sequence[str], on_change: Callable[[], None] | None = None) -> None:
        self.engine = engine
        self.theme = theme
        self.on_change = on_change
        self.metric_keys = list(dict.fromkeys(metrics))

        self.win = tk.Toplevel(parent)
        self.win.title("gpumon - alarm thresholds")
        self.win.configure(bg=W.BG)
        self.win.geometry("760x620")
        self.win.transient(parent)
        self.win.grab_set()

        tk.Label(self.win, text="Alarm thresholds", bg=W.BG, fg=W.TEXT_BRIGHT,
                 font=theme.title, anchor="w").pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(self.win, text="Leave a field blank to disable that level. "
                                "Hysteresis is how far a value must fall below the "
                                "threshold before the alarm clears. Minimum duration "
                                "ignores single-sample spikes.",
                 bg=W.BG, fg=W.TEXT_DIM, font=theme.small, anchor="w",
                 wraplength=720, justify="left").pack(anchor="w", padx=12, pady=(2, 8))

        body = tk.Frame(self.win, bg=W.BG)
        body.pack(fill="both", expand=True, padx=12)

        headers = [("metric", 26), ("unit", 6), ("warning", 10), ("critical", 10),
                   ("hysteresis", 11), ("min dur (s)", 11), ("enabled", 8)]
        head = tk.Frame(body, bg=W.BG)
        head.pack(fill="x")
        for text, width in headers:
            tk.Label(head, text=text, bg=W.BG, fg=W.TEXT_DIM, font=theme.small,
                     width=width, anchor="w").pack(side="left")

        self.entries: dict[str, dict[str, tk.Entry]] = {}
        self.vars: dict[str, tk.BooleanVar] = {}

        canvas = tk.Canvas(body, bg=W.BG, highlightthickness=0, bd=0)
        vbar = W.ThemedScrollbar(body, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=W.BG)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")
        inner.bind("<Configure>",
                   lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win_id, width=e.width))

        for row, key in enumerate(self.metric_keys):
            rule = engine.rules.get(key) or A.default_rules_for(key)
            d = M.metric_def(key)
            gpu = M.gpu_index_of(key)
            label = f"gpu{gpu} {d.label}" if gpu is not None else d.label
            bg = W.PANEL if row % 2 else W.PANEL_ALT
            line = tk.Frame(inner, bg=bg)
            line.pack(fill="x")
            tk.Label(line, text=label, bg=bg, fg=W.TEXT, font=theme.small,
                     width=26, anchor="w").pack(side="left")
            tk.Label(line, text=d.unit or "-", bg=bg, fg=W.TEXT_DIM,
                     font=theme.small, width=6, anchor="w").pack(side="left")

            fields: dict[str, tk.Entry] = {}
            for field, value, width in (("warning", rule.warning, 10),
                                        ("critical", rule.critical, 10),
                                        ("hysteresis", rule.hysteresis, 11),
                                        ("min_duration", rule.min_duration, 11)):
                entry = tk.Entry(line, width=width, bg=W.PANEL_ALT, fg=W.TEXT,
                                 insertbackground=W.TEXT, relief="flat",
                                 font=theme.small, justify="right")
                entry.insert(0, "" if value is None else f"{value:g}")
                entry.pack(side="left", padx=(0, 2))
                fields[field] = entry
            var = tk.BooleanVar(value=rule.enabled)
            tk.Checkbutton(line, variable=var, bg=bg, activebackground=bg,
                           selectcolor=W.PANEL_ALT, highlightthickness=0,
                           bd=0).pack(side="left", padx=6)
            self.entries[key] = fields
            self.vars[key] = var

        buttons = tk.Frame(self.win, bg=W.BG)
        buttons.pack(fill="x", padx=12, pady=10)
        self.status = tk.Label(buttons, text="", bg=W.BG, fg=W.TEXT_DIM,
                               font=theme.small)
        self.status.pack(side="left")
        W.FlatButton(buttons, " CANCEL", self.win.destroy, theme=theme).pack(side="right")
        W.FlatButton(buttons, " SAVE", self.save,
                     bg=W.action_colours("ok")[0], fg=W.OK,
                     theme=theme).pack(side="right", padx=6)
        W.FlatButton(buttons, " RESET DEFAULTS", self.reset_defaults,
                     theme=theme).pack(side="right", padx=6)

    def reset_defaults(self) -> None:
        for key, fields in self.entries.items():
            base = A.default_rules_for(key)
            for name, value in (("warning", base.warning), ("critical", base.critical),
                                ("hysteresis", base.hysteresis),
                                ("min_duration", base.min_duration)):
                fields[name].delete(0, "end")
                fields[name].insert(0, "" if value is None else f"{value:g}")
            self.vars[key].set(base.enabled)
        self.status.configure(text="defaults restored (not saved yet)", fg=W.WARN)

    def save(self) -> None:
        problems: list[str] = []
        parsed: dict[str, A.AlarmRule] = {}
        for key, fields in self.entries.items():
            values: dict[str, float | None] = {}
            for name, entry in fields.items():
                raw = entry.get().strip()
                if raw == "":
                    if name in ("hysteresis", "min_duration"):
                        values[name] = 0.0
                    else:
                        values[name] = None
                    continue
                try:
                    values[name] = float(raw)
                except ValueError:
                    problems.append(f"{key}.{name}: '{raw}' is not a number")
                    values[name] = None
            if problems:
                continue
            if (values.get("warning") is not None and values.get("critical") is not None
                    and values["critical"] < values["warning"]):
                problems.append(f"{key}: critical is below warning")
                continue
            parsed[key] = A.AlarmRule(
                metric=key, warning=values.get("warning"),
                critical=values.get("critical"),
                hysteresis=values.get("hysteresis") or 0.0,
                min_duration=values.get("min_duration") or 0.0,
                enabled=self.vars[key].get())

        if problems:
            self.status.configure(text="; ".join(problems[:3]), fg=W.CRIT)
            return

        for rule in parsed.values():
            self.engine.set_rule(rule)
        if self.on_change:
            self.on_change()
        self.status.configure(text=f"saved {len(parsed)} rules", fg=W.OK)
        self.win.after(500, self.win.destroy)
