"""Alarm rules over the metric stream.

An alarm opens when a value crosses `threshold` (plus `hysteresis`) and closes
when it falls back below `threshold - hysteresis`. `min_duration` suppresses
single-sample spikes, which matters for GPU hotspots that blip for one tick.

Every open/close is persisted as an `alarm_event` row by the sampler, so the
timeline in the summary report can show exactly when thresholds were crossed.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Literal

from metrics import metric_def, gpu_index_of, gpu_field_of

Level = Literal["warning", "critical"]
State = Literal["ok", "warning", "critical"]


@dataclass
class AlarmRule:
    metric: str
    warning: float | None = None
    critical: float | None = None
    hysteresis: float = 1.0
    min_duration: float = 0.0
    enabled: bool = True

    def level_for(self, value: float) -> Level | None:
        if not self.enabled:
            return None
        if self.critical is not None and value >= self.critical:
            return "critical"
        if self.warning is not None and value >= self.warning:
            return "warning"
        return None

    @property
    def threshold_for(self) -> dict[str, float]:
        out: dict[str, float] = {}
        if self.warning is not None:
            out["warning"] = self.warning
        if self.critical is not None:
            out["critical"] = self.critical
        return out


@dataclass
class ActiveAlarm:
    """An alarm currently open, tracked so the peak can be recorded."""
    metric: str
    level: Level
    threshold: float
    started_t: float
    started_wall: float
    peak: float
    samples: int = 0
    alarm_id: int | None = None      # DB row id once flushed
    pending: bool = True             # still within min_duration window

    @property
    def gpu_index(self) -> int | None:
        return gpu_index_of(self.metric)

    @property
    def label(self) -> str:
        g = gpu_index_of(self.metric)
        d = metric_def(self.metric)
        prefix = f"GPU{g} " if g is not None else ""
        return f"{prefix}{d.label}"


@dataclass
class AlarmUpdate:
    """Emitted when an alarm opens, escalates, or closes."""
    kind: Literal["open", "escalate", "close"]
    alarm: ActiveAlarm
    t: float
    wall: float


DEFAULT_RULES: dict[str, AlarmRule] = {}


def _default(metric: str, warning: float | None, critical: float | None,
             hysteresis: float = 1.0, min_duration: float = 0.0) -> None:
    DEFAULT_RULES[metric] = AlarmRule(
        metric, warning, critical, hysteresis, min_duration)


# GPU defaults are conservative: NVIDIA's default throttle point is ~83-84 C.
_default("temp", 80.0, 88.0, hysteresis=2.0, min_duration=1.0)
_default("hotspot", 95.0, 105.0, hysteresis=2.0, min_duration=1.0)
_default("mem_temp", 95.0, 105.0, hysteresis=2.0, min_duration=1.0)
_default("vram_percent", 95.0, 99.0, hysteresis=1.0)
_default("fan", None, None)
_default("util", None, None)
_default("cpu_temp", 85.0, 95.0, hysteresis=2.0, min_duration=2.0)
_default("ram_percent", 90.0, 97.0, hysteresis=1.0)


def default_rules_for(metric: str) -> AlarmRule:
    """Resolve a template rule onto a concrete metric key (gpuN_temp etc.)."""
    if metric in DEFAULT_RULES:
        base = DEFAULT_RULES[metric]
    elif gpu_field_of(metric) in DEFAULT_RULES:
        base = DEFAULT_RULES[gpu_field_of(metric)]
    else:
        return AlarmRule(metric)
    return AlarmRule(metric, base.warning, base.critical, base.hysteresis,
                     base.min_duration, base.enabled)


class AlarmEngine:
    """Evaluates rules against successive samples, with hysteresis and debounce."""

    def __init__(self) -> None:
        self.rules: dict[str, AlarmRule] = {}
        self.active: dict[str, ActiveAlarm] = {}
        self.history: list[AlarmUpdate] = []
        self._pending_since: dict[str, float] = {}

    # -- configuration ---------------------------------------------------
    def set_rule(self, rule: AlarmRule) -> None:
        self.rules[rule.metric] = rule

    def configure(self, metrics: Iterable[str],
                  overrides: dict[str, AlarmRule] | None = None) -> None:
        """Install default rules for `metrics`, then apply any overrides."""
        for metric in metrics:
            if overrides and metric in overrides:
                self.rules[metric] = overrides[metric]
            elif metric not in self.rules:
                rule = default_rules_for(metric)
                if rule.warning is not None or rule.critical is not None:
                    self.rules[metric] = rule
        if overrides:
            for metric, rule in overrides.items():
                self.rules[metric] = rule

    def export(self) -> dict[str, Any]:
        return {k: asdict(v) for k, v in sorted(self.rules.items())}

    @classmethod
    def from_config(cls, raw: dict[str, Any] | None) -> "AlarmEngine":
        engine = cls()
        for metric, cfg in (raw or {}).items():
            if not isinstance(cfg, dict):
                continue
            engine.set_rule(AlarmRule(
                metric=metric,
                warning=cfg.get("warning"),
                critical=cfg.get("critical"),
                hysteresis=float(cfg.get("hysteresis", 1.0)),
                min_duration=float(cfg.get("min_duration", 0.0)),
                enabled=bool(cfg.get("enabled", True))))
        return engine

    # -- evaluation ------------------------------------------------------
    def evaluate(self, t: float, wall: float,
                 values: dict[str, float]) -> list[AlarmUpdate]:
        """Feed one sample; returns state transitions to persist/display."""
        updates: list[AlarmUpdate] = []
        for metric, rule in self.rules.items():
            value = values.get(metric)
            if value is None:
                continue
            updates.extend(self._evaluate_one(metric, rule, value, t, wall))
        return updates

    def _evaluate_one(self, metric: str, rule: AlarmRule, value: float,
                      t: float, wall: float) -> list[AlarmUpdate]:
        updates: list[AlarmUpdate] = []
        current = self.active.get(metric)
        level = rule.level_for(value)

        if current is None:
            if level is None:
                return updates
            threshold = (rule.critical if level == "critical" else rule.warning)
            assert threshold is not None
            candidate = ActiveAlarm(
                metric=metric, level=level, threshold=threshold, started_t=t,
                started_wall=wall, peak=value, samples=1)
            if rule.min_duration > 0:
                # Hold the alarm until it has persisted long enough.
                self._pending_since[metric] = t
                self.active[metric] = candidate
                candidate.pending = True
            else:
                candidate.pending = False
                self.active[metric] = candidate
                updates.append(AlarmUpdate("open", candidate, t, wall))
            return updates

        # Already active: update peak and decide whether it continues.
        current.peak = max(current.peak, value)
        current.samples += 1
        release = current.threshold - rule.hysteresis

        if level is None and value < release:
            del self.active[metric]
            self._pending_since.pop(metric, None)
            if current.pending:
                # Never lasted long enough to count: drop it silently.
                return updates
            updates.append(AlarmUpdate("close", current, t, wall))
            return updates

        if current.pending:
            started = self._pending_since.get(metric, t)
            if t - started >= rule.min_duration:
                current.pending = False
                # Backdate the open to the first crossing so the timeline is honest.
                current.started_t = started
                updates.append(AlarmUpdate("open", current, t, wall))
            return updates

        # Escalation from warning to critical.
        if level == "critical" and current.level == "warning" and rule.critical is not None:
            updates.append(AlarmUpdate("close", current, t, wall))
            escalated = ActiveAlarm(
                metric=metric, level="critical", threshold=rule.critical,
                started_t=t, started_wall=wall, peak=value, samples=1, pending=False)
            self.active[metric] = escalated
            updates.append(AlarmUpdate("open", escalated, t, wall))
        return updates

    def force_close_all(self, t: float, wall: float) -> list[AlarmUpdate]:
        updates: list[AlarmUpdate] = []
        for metric, alarm in list(self.active.items()):
            if alarm.pending:
                del self.active[metric]
                continue
            alarm.pending = False
            updates.append(AlarmUpdate("close", alarm, t, wall))
        self.active.clear()
        return updates

    def reset(self) -> None:
        self.active.clear()
        self._pending_since.clear()

    @property
    def worst_level(self) -> State:
        if any(a.level == "critical" for a in self.active.values()):
            return "critical"
        if self.active:
            return "warning"
        return "ok"

    def active_for(self, metric: str) -> ActiveAlarm | None:
        return self.active.get(metric)


# --------------------------------------------------------------------------
# Config file
# --------------------------------------------------------------------------


def config_path() -> str:
    """Where settings live. `GPUMON_CONFIG` overrides it.

    The override exists so a test run, a second instance or a trial of a change
    can be pointed at a scratch file. Without it every test that touches a
    setting writes the one file the user's real alarms and theme live in - which
    is exactly what happened while the theme tests were being written.

    `apppaths.app_dir()` rather than this module's folder: a frozen build unpacks
    into a temporary directory, and settings written there would be gone by the
    next launch.
    """
    override = os.environ.get("GPUMON_CONFIG")
    if override:
        return override
    import apppaths
    return apppaths.app_path("config.json")


def load_config(path: str | None = None) -> dict[str, Any]:
    path = path or config_path()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(config: dict[str, Any], path: str | None = None) -> None:
    path = path or config_path()
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def rules_to_config(engine: AlarmEngine) -> dict[str, Any]:
    """Only persist rules the user actually changed from the defaults."""
    out: dict[str, Any] = {}
    for metric, rule in engine.rules.items():
        base = default_rules_for(metric)
        changed = (rule.warning != base.warning or rule.critical != base.critical
                   or rule.hysteresis != base.hysteresis
                   or rule.min_duration != base.min_duration
                   or rule.enabled != base.enabled)
        if changed:
            out[metric] = asdict(rule)
    return out
