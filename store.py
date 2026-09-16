"""Durable storage for gpumon sessions.

Design notes
------------
* SQLite in WAL mode: the sampler thread writes while the UI reads, and a
  benchmark run is never lost if the app is killed mid-test.
* Samples are stored *narrow* (one row per metric per tick). That keeps the
  schema stable when new sensors appear, makes per-metric aggregate queries
  trivial, and lets the summary reuse the same SQL the live view needs.
* Alarm crossings are separate rows referencing the sample they were detected
  on, so the timeline survives even if the threshold config changes later.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Iterator, Sequence

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    REAL    NOT NULL,
    ended_at      REAL,
    label         TEXT    NOT NULL DEFAULT '',
    notes         TEXT    NOT NULL DEFAULT '',
    sample_hz     REAL    NOT NULL,
    device_json   TEXT    NOT NULL DEFAULT '[]',
    capability_json TEXT  NOT NULL DEFAULT '{}',
    config_json   TEXT    NOT NULL DEFAULT '{}',
    app_version   TEXT    NOT NULL DEFAULT '',
    sample_count  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sample (
    session_id  INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    t           REAL    NOT NULL,      -- seconds since session start
    wall        REAL    NOT NULL,      -- absolute unix time
    metric      TEXT    NOT NULL,
    value       REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sample_session_metric_t
    ON sample(session_id, metric, t);
CREATE INDEX IF NOT EXISTS ix_sample_session_t
    ON sample(session_id, t);

CREATE TABLE IF NOT EXISTS alarm_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    metric      TEXT    NOT NULL,
    level       TEXT    NOT NULL,      -- warning | critical
    threshold   REAL    NOT NULL,
    started_t   REAL    NOT NULL,      -- seconds since session start
    started_wall REAL   NOT NULL,
    ended_t     REAL,
    ended_wall  REAL,
    peak        REAL    NOT NULL,
    samples     INTEGER NOT NULL DEFAULT 0,
    gpu_index   INTEGER,
    message     TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_alarm_session ON alarm_event(session_id, started_t);

CREATE TABLE IF NOT EXISTS marker (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
    t          REAL    NOT NULL,
    wall       REAL    NOT NULL,
    label      TEXT    NOT NULL,
    kind       TEXT    NOT NULL DEFAULT 'user'
);
"""


@dataclass
class DeviceRecord:
    index: int
    name: str
    vendor: str = "unknown"
    vram_total_mb: float | None = None
    sources: list[str] = field(default_factory=list)
    note: str = ""
    #: PCI bus:device.function - the only way to tell two identical cards apart
    #: in a report.
    pci: str = ""
    device_key: str = ""


@dataclass
class SessionRecord:
    id: int
    started_at: float
    ended_at: float | None
    label: str
    notes: str
    sample_hz: float
    devices: list[DeviceRecord]
    capabilities: dict[str, Any]
    config: dict[str, Any]
    app_version: str
    sample_count: int

    @property
    def duration(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.time()
        return max(0.0, end - self.started_at)


@dataclass
class AlarmEvent:
    id: int
    metric: str
    level: str
    threshold: float
    started_t: float
    ended_t: float | None
    peak: float
    samples: int
    gpu_index: int | None
    message: str

    @property
    def duration(self) -> float | None:
        if self.ended_t is None:
            return None
        return max(0.0, self.ended_t - self.started_t)

    @property
    def ongoing(self) -> bool:
        return self.ended_t is None


@dataclass
class MetricStats:
    metric: str
    count: int
    minimum: float
    maximum: float
    mean: float
    median: float
    p95: float
    p99: float
    stdev: float
    first: float
    last: float
    duration: float


class Store:
    """SQLite-backed session storage. Thread-safe via a lock plus WAL.

    Commits are batched by time. Committing on every sample makes the sampler
    block on a filesystem flush each tick, which under heavy CPU/disk load (the
    exact conditions this tool measures) stalls sampling for seconds. Batching
    bounds the worst-case data loss to `commit_interval` seconds while keeping
    the sampler's per-tick cost at a memory write.
    """

    #: Flush pending samples to disk at most this often.
    COMMIT_INTERVAL = 1.0

    def __init__(self, path: str, commit_interval: float | None = None) -> None:
        self.path = path
        if commit_interval is not None:
            self.COMMIT_INTERVAL = commit_interval
        directory = os.path.dirname(os.path.abspath(path))
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=15.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._dirty = False
        self._last_commit = 0.0
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),))
            self._conn.commit()
            self._last_commit = time.monotonic()

    # -- commit batching -------------------------------------------------
    def _commit_locked(self) -> None:
        self._conn.commit()
        self._dirty = False
        self._last_commit = time.monotonic()

    def flush(self) -> None:
        """Force pending writes to disk."""
        with self._lock:
            if self._dirty:
                self._commit_locked()

    def _maybe_commit_locked(self, force: bool = False) -> None:
        if not self._dirty:
            return
        if force or (time.monotonic() - self._last_commit) >= self.COMMIT_INTERVAL:
            self._commit_locked()

    def commit_age(self) -> float:
        """Seconds of writes not yet flushed to disk."""
        with self._lock:
            if not self._dirty:
                return 0.0
            return time.monotonic() - self._last_commit

    # -- sessions --------------------------------------------------------
    def create_session(self, *, label: str, sample_hz: float,
                       devices: Sequence[DeviceRecord],
                       capabilities: dict[str, Any],
                       config: dict[str, Any],
                       app_version: str,
                       started_at: float | None = None) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO session(started_at, label, sample_hz, device_json,"
                " capability_json, config_json, app_version)"
                " VALUES(?,?,?,?,?,?,?)",
                (started_at if started_at is not None else time.time(), label,
                 sample_hz, json.dumps([asdict(d) for d in devices]),
                 json.dumps(capabilities), json.dumps(config), app_version))
            session_id = int(cur.lastrowid)
            self._commit_locked()
            return session_id

    def finish_session(self, session_id: int, ended_at: float | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE session SET ended_at=? WHERE id=? AND ended_at IS NULL",
                (ended_at if ended_at is not None else time.time(), session_id))
            self._conn.execute(
                "UPDATE session SET sample_count="
                "(SELECT COUNT(DISTINCT t) FROM sample WHERE session_id=?) WHERE id=?",
                (session_id, session_id))
            # Always durable at the end of a run.
            self._commit_locked()

    def delete_session(self, session_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM session WHERE id=?", (session_id,))
            self._commit_locked()

    def get_session(self, session_id: int) -> SessionRecord | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM session WHERE id=?", (session_id,)).fetchone()
        return self._row_to_session(row) if row else None

    def list_sessions(self, limit: int = 100) -> list[SessionRecord]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM session ORDER BY started_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [self._row_to_session(r) for r in rows]

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> SessionRecord:
        try:
            devices_raw = json.loads(row["device_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            devices_raw = []
        devices = []
        for d in devices_raw:
            if isinstance(d, dict):
                devices.append(DeviceRecord(
                    index=int(d.get("index", 0)), name=str(d.get("name", "")),
                    vendor=str(d.get("vendor", "")),
                    vram_total_mb=d.get("vram_total_mb"),
                    sources=list(d.get("sources") or []),
                    note=str(d.get("note", "")),
                    pci=str(d.get("pci", "")),
                    device_key=str(d.get("device_key", ""))))
        return SessionRecord(
            id=int(row["id"]), started_at=float(row["started_at"]),
            ended_at=(float(row["ended_at"]) if row["ended_at"] is not None else None),
            label=row["label"] or "", notes=row["notes"] or "",
            sample_hz=float(row["sample_hz"]), devices=devices,
            capabilities=_safe_json(row["capability_json"]),
            config=_safe_json(row["config_json"]),
            app_version=row["app_version"] or "",
            sample_count=int(row["sample_count"] or 0))

    # -- samples ---------------------------------------------------------
    def write_samples(self, session_id: int, t: float, wall: float,
                      values: dict[str, float]) -> None:
        if not values:
            return
        rows = [(session_id, t, wall, k, float(v)) for k, v in values.items()
                if v is not None]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO sample(session_id, t, wall, metric, value)"
                " VALUES(?,?,?,?,?)", rows)
            self._dirty = True
            self._maybe_commit_locked()

    def metrics_for_session(self, session_id: int) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT metric FROM sample WHERE session_id=? ORDER BY metric",
                (session_id,)).fetchall()
        return [r["metric"] for r in rows]

    def series(self, session_id: int, metric: str,
               t0: float | None = None, t1: float | None = None) -> list[tuple[float, float]]:
        sql = "SELECT t, value FROM sample WHERE session_id=? AND metric=?"
        args: list[Any] = [session_id, metric]
        if t0 is not None:
            sql += " AND t>=?"
            args.append(t0)
        if t1 is not None:
            sql += " AND t<=?"
            args.append(t1)
        sql += " ORDER BY t"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [(float(r["t"]), float(r["value"])) for r in rows]

    def sample_span(self, session_id: int, t0: float | None = None,
                    t1: float | None = None) -> tuple[float, float] | None:
        sql = "SELECT MIN(t) a, MAX(t) b FROM sample WHERE session_id=?"
        args: list[Any] = [session_id]
        if t0 is not None:
            sql += " AND t>=?"
            args.append(t0)
        if t1 is not None:
            sql += " AND t<=?"
            args.append(t1)
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        if row is None or row["a"] is None:
            return None
        return float(row["a"]), float(row["b"])

    def peak_concurrency(self, session_id: int) -> int:
        """Distinct timestamps recorded - used to report the effective rate."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(DISTINCT t) c FROM sample WHERE session_id=?",
                (session_id,)).fetchone()
        return int(row["c"]) if row else 0

    # -- statistics ------------------------------------------------------
    def stats_range(self, session_id: int) -> tuple[float, float] | None:
        """Alias of sample_span, named for the summary's window selector."""
        return self.sample_span(session_id)

    def stats(self, session_id: int, metric: str,
              t0: float | None = None, t1: float | None = None) -> MetricStats | None:
        """Statistics for one metric, optionally limited to [t0, t1]."""
        sql = "SELECT t, value FROM sample WHERE session_id=? AND metric=?"
        args: list[Any] = [session_id, metric]
        if t0 is not None:
            sql += " AND t>=?"
            args.append(t0)
        if t1 is not None:
            sql += " AND t<=?"
            args.append(t1)
        sql += " ORDER BY value"
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        values = [float(r["value"]) for r in rows]
        if not values:
            return None
        n = len(values)
        mean = sum(values) / n
        var = sum((v - mean) ** 2 for v in values) / n
        first_last = self._first_last(session_id, metric, t0, t1)
        span = self.sample_span(session_id, t0, t1)
        return MetricStats(
            metric=metric, count=n, minimum=values[0], maximum=values[-1],
            mean=mean, median=_percentile(values, 50.0),
            p95=_percentile(values, 95.0), p99=_percentile(values, 99.0),
            stdev=var ** 0.5, first=first_last[0], last=first_last[1],
            duration=(span[1] - span[0]) if span else 0.0)

    def _first_last(self, session_id: int, metric: str,
                    t0: float | None = None,
                    t1: float | None = None) -> tuple[float, float]:
        """First and last value in the window, taken by time not by magnitude."""
        series = self.series(session_id, metric, t0, t1)
        if not series:
            return (0.0, 0.0)
        return (series[0][1], series[-1][1])

    def stats_many(self, session_id: int, t0: float | None = None,
                   t1: float | None = None) -> dict[str, MetricStats]:
        out: dict[str, MetricStats] = {}
        for metric in self.metrics_for_session(session_id):
            st = self.stats(session_id, metric, t0, t1)
            if st:
                out[metric] = st
        return out

    def time_above(self, session_id: int, metric: str, threshold: float,
                   t0: float | None = None, t1: float | None = None) -> float:
        """Seconds spent at or above `threshold`, estimated from sample spacing."""
        series = self.series(session_id, metric, t0, t1)
        return _time_above(series, threshold)

    # -- alarms ----------------------------------------------------------
    def add_alarm(self, session_id: int, *, metric: str, level: str, threshold: float,
                  started_t: float, started_wall: float, peak: float,
                  gpu_index: int | None, message: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO alarm_event(session_id, metric, level, threshold,"
                " started_t, started_wall, peak, samples, gpu_index, message)"
                " VALUES(?,?,?,?,?,?,?,1,?,?)",
                (session_id, metric, level, threshold, started_t, started_wall,
                 peak, gpu_index, message))
            alarm_id = int(cur.lastrowid)
            # Alarm transitions are the timeline's anchor points: persist at once.
            self._commit_locked()
            return alarm_id

    def update_alarm(self, alarm_id: int, *, ended_t: float | None, ended_wall: float | None,
                     peak: float, samples: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE alarm_event SET ended_t=?, ended_wall=?, peak=?, samples=?"
                " WHERE id=?", (ended_t, ended_wall, peak, samples, alarm_id))
            self._commit_locked()

    def alarms(self, session_id: int) -> list[AlarmEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM alarm_event WHERE session_id=? ORDER BY started_t",
                (session_id,)).fetchall()
        return [AlarmEvent(
            id=int(r["id"]), metric=r["metric"], level=r["level"],
            threshold=float(r["threshold"]), started_t=float(r["started_t"]),
            ended_t=(float(r["ended_t"]) if r["ended_t"] is not None else None),
            peak=float(r["peak"]), samples=int(r["samples"]),
            gpu_index=(int(r["gpu_index"]) if r["gpu_index"] is not None else None),
            message=r["message"] or "") for r in rows]

    # -- markers ---------------------------------------------------------
    def add_marker(self, session_id: int, t: float, wall: float, label: str,
                   kind: str = "user") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO marker(session_id, t, wall, label, kind) VALUES(?,?,?,?,?)",
                (session_id, t, wall, label, kind))
            self._commit_locked()

    def markers(self, session_id: int) -> list[tuple[float, float, str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT t, wall, label, kind FROM marker WHERE session_id=? ORDER BY t",
                (session_id,)).fetchall()
        return [(float(r["t"]), float(r["wall"]), r["label"], r["kind"]) for r in rows]

    def close(self) -> None:
        with self._lock:
            try:
                self._commit_locked()
            except sqlite3.Error:
                pass
            self._conn.close()


# --------------------------------------------------------------------------
# Statistics helpers (kept free of numpy so the app has no hard dependency)
# --------------------------------------------------------------------------


def _percentile(sorted_values: Sequence[float], pct: float) -> float:
    """Linear-interpolation percentile over already-sorted values."""
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    ordered = sorted(sorted_values)
    k = (len(ordered) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    frac = k - lo
    return float(ordered[lo] * (1 - frac) + ordered[hi] * frac)


def _time_above(series: Sequence[tuple[float, float]], threshold: float) -> float:
    """Seconds where value >= threshold, weighting each sample by its interval."""
    if not series:
        return 0.0
    total = 0.0
    for i, (t, v) in enumerate(series):
        if v < threshold:
            continue
        if i + 1 < len(series):
            dt = series[i + 1][0] - t
        else:
            dt = (series[i][0] - series[i - 1][0]) if i > 0 else 0.0
        total += max(0.0, dt)
    return total


def _safe_json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        got = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return got if isinstance(got, dict) else {}


def default_db_path() -> str:
    base = os.path.join(os.path.expanduser("~"), "Documents", "gpumon")
    return os.path.join(base, "sessions.db")


def human_duration(seconds: float | None) -> str:
    if seconds is None:
        return "ongoing"
    seconds = max(0.0, seconds)
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{int(minutes)}m {sec:04.1f}s"
    hours, minutes = divmod(int(minutes), 60)
    return f"{hours}h {minutes:02d}m {sec:04.1f}s"
