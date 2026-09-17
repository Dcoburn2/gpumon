"""Sampling engine: one thread polls sensors, feeds alarms and persists logging.

Runs continuously so the live view always has data; logging to SQLite is a
separate toggle (`start_logging` / `stop_logging`). The thread never blocks on
the UI: results are handed to callbacks, and the UI drains them on its own tick.
"""
from __future__ import annotations

import collections
import ctypes
import os
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

import alarms as A
import metrics as M
import store as S

APP_VERSION = "1.0.1"

# How much history the live view keeps for its scrolling graphs.
LIVE_WINDOW_SECONDS = 180.0


# --------------------------------------------------------------------------
# Thread priority
# --------------------------------------------------------------------------

# A benchmark saturating every core (Cinebench, FurMark's CPU side, a solver)
# leaves the sampler competing as an equal with CPU-bound worker threads, and
# measured sampling dropped to roughly one sample per 10 s. Raising the sampler
# a single notch above normal keeps the sample cadence honest without letting it
# outrank interactive work. Python 3.14 still has no thread-priority API, hence
# the ctypes call.
THREAD_PRIORITY_ABOVE_NORMAL = 1


def raise_thread_priority() -> bool:
    """Nudge the calling thread above normal priority. Returns success."""
    if os.name != "nt":
        try:
            os.nice(-5)
            return True
        except (AttributeError, PermissionError, OSError):
            return False
    try:
        kernel32 = ctypes.WinDLL("kernel32.dll")
        kernel32.GetCurrentThread.restype = ctypes.c_void_p
        kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
        kernel32.SetThreadPriority.restype = ctypes.c_int
        return bool(kernel32.SetThreadPriority(
            kernel32.GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL))
    except (OSError, AttributeError):
        return False


# --------------------------------------------------------------------------
# Asynchronous sample writer
# --------------------------------------------------------------------------


class SampleWriter:
    """Drains queued samples to SQLite on its own thread.

    The sampler must never wait on disk. Measured on this workstation (Balanced
    power plan, 5% minimum processor state) a 0.7 ms SQLite insert becomes ~10 s
    when every core is saturated, because the writing thread has to wait for
    cores to leave deep sleep states. If the sampler wrote inline it would lose
    almost every sample during precisely the benchmark it exists to measure.

    Queueing decouples the two: sampling stays on schedule, and if the disk
    falls behind we drop the oldest queued samples and report it rather than
    corrupting the sample cadence.
    """

    def __init__(self, store: S.Store, capacity: int = 4096) -> None:
        self.store = store
        self.capacity = capacity
        self._queue: queue.Queue[tuple[int, float, float, dict[str, float]]] = \
            queue.Queue(maxsize=capacity)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.written = 0
        self.dropped = 0
        self.last_error = ""
        self.last_write_ms = 0.0

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="gpumon-writer",
                                            daemon=True)
            self._thread.start()

    def submit(self, session_id: int, t: float, wall: float,
               values: dict[str, float]) -> bool:
        """Queue a sample. Never blocks; drops the oldest entry when full."""
        item = (session_id, t, wall, values)
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            pass
        try:
            self._queue.get_nowait()          # discard the oldest
            self.dropped += 1
        except queue.Empty:
            pass
        try:
            self._queue.put_nowait(item)
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def drain(self, timeout: float = 20.0) -> bool:
        """Wait until the queue is empty. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty():
                return True
            time.sleep(0.02)
        return self._queue.empty()

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    def stop(self, timeout: float = 20.0) -> None:
        self.drain(timeout=timeout)
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        while True:
            item = self._next_item()
            if item is None:
                if self._stop.is_set() and self._queue.empty():
                    return
                continue
            session_id, t, wall, values = item
            t0 = time.perf_counter()
            try:
                self.store.write_samples(session_id, t, wall, values)
                self.written += 1
                self.last_error = ""
            except Exception as exc:  # noqa: BLE001 - never kill the writer
                self.last_error = f"store write failed: {exc}"
            self.last_write_ms = (time.perf_counter() - t0) * 1000.0

    def _next_item(self) -> tuple[int, float, float, dict[str, float]] | None:
        try:
            return self._queue.get(timeout=0.2)
        except queue.Empty:
            return None


# --------------------------------------------------------------------------
# Sampling engine
# --------------------------------------------------------------------------


class LiveBuffer:
    """Fixed-window ring of recent samples for the live graphs.

    Samples arrive in time order; we keep the newest `capacity` and let readers
    slice a time window cheaply.
    """

    def __init__(self, capacity: int = 5400) -> None:
        self.capacity = capacity
        self._t: collections.deque[float] = collections.deque(maxlen=capacity)
        self._v: dict[str, collections.deque[float]] = {}
        self._lock = threading.Lock()

    def append(self, t: float, values: dict[str, float]) -> None:
        with self._lock:
            self._t.append(t)
            for key in list(self._v):
                if key not in values:
                    self._v[key].append(float("nan"))
            for key, val in values.items():
                dq = self._v.get(key)
                if dq is None:
                    # Backfill so every series aligns with the time axis.
                    dq = collections.deque([float("nan")] * (len(self._t) - 1),
                                           maxlen=self.capacity)
                    self._v[key] = dq
                dq.append(float(val))

    def window(self, metric: str, seconds: float) -> list[tuple[float, float]]:
        """(t, value) pairs within the last `seconds`, NaN values dropped."""
        with self._lock:
            if not self._t:
                return []
            dq = self._v.get(metric)
            if not dq:
                return []
            times = list(self._t)
            values = list(dq)
        cutoff = times[-1] - seconds
        out: list[tuple[float, float]] = []
        for t, v in zip(times, values):
            if t < cutoff:
                continue
            if v != v:  # NaN
                continue
            out.append((t, v))
        return out

    def latest(self, metric: str) -> float | None:
        with self._lock:
            dq = self._v.get(metric)
            if not dq:
                return None
        for v in reversed(dq):
            if v == v:
                return v
        return None

    def span(self) -> tuple[float, float] | None:
        with self._lock:
            if not self._t:
                return None
            return self._t[0], self._t[-1]

    def clear(self) -> None:
        with self._lock:
            self._t.clear()
            self._v.clear()


@dataclass
class SessionSummary:
    session_id: int
    label: str
    started_at: float
    ended_at: float
    duration: float
    samples: int
    alarms: list[S.AlarmEvent] = field(default_factory=list)
    db_path: str = ""


@dataclass
class SamplerStatus:
    sample_hz: float
    effective_hz: float
    poll_ms: float
    dropped: int
    last_error: str
    logging: bool
    session_id: int | None
    elapsed: float
    samples_written: int
    gap_count: int


class Sampler:
    """Background sensor poller with optional durable logging.

    Callbacks are invoked on the sampler thread, so the UI must only enqueue
    from them (the tkinter UI polls a queue instead of touching widgets).
    """

    def __init__(self, manager: M.SensorManager, store: S.Store, *,
                 sample_hz: float = 1.0, alarm_engine: A.AlarmEngine | None = None,
                 per_core: bool = False) -> None:
        self.manager = manager
        self.store = store
        self.sample_hz = max(0.2, min(10.0, sample_hz))
        self.alarms = alarm_engine or A.AlarmEngine()
        self.per_core = per_core
        self.live = LiveBuffer(int(LIVE_WINDOW_SECONDS * self.sample_hz) + 64)
        # Disk writes run on their own thread so sampling never waits on I/O.
        self.writer = SampleWriter(store)

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        # logging state
        self.session_id: int | None = None
        self._session_start_mono: float = 0.0
        #: Timestamp of the most recent sampling tick, so a session can be
        #: anchored on it rather than on a moment measured mid-poll.
        self._last_tick_mono: float = 0.0
        self._session_start_wall: float = 0.0
        self._label = ""
        self._samples_written = 0

        # telemetry about the sampler itself
        self._effective_hz = 0.0
        self._poll_ms = 0.0
        self._dropped = 0
        self._last_error = ""
        self._gap_count = 0
        self._last_tick_mono = 0.0
        self._priority_raised = False

        self.on_sample: Callable[[float, dict[str, float]], None] | None = None
        self.on_alarm: Callable[[A.AlarmUpdate], None] | None = None
        self.on_error: Callable[[str], None] | None = None

        self._recent_alarms: collections.deque[A.AlarmUpdate] = collections.deque(maxlen=500)

        # detected metrics feed the alarm defaults
        self.alarms.configure(self._candidate_metrics())

    # -- configuration ---------------------------------------------------
    def _candidate_metrics(self) -> list[str]:
        keys: list[str] = ["cpu_temp", "cpu_util", "ram_percent"]
        for g in self.manager.gpus:
            keys += [f"gpu{g.index}_{f}" for f in
                     ("temp", "hotspot", "mem_temp", "vram_percent")]
        return keys

    def set_sample_hz(self, hz: float) -> None:
        with self._lock:
            self.sample_hz = max(0.2, min(10.0, hz))

    def configure_alarms(self, overrides: dict[str, A.AlarmRule] | None = None) -> None:
        """Install alarm defaults for the detected sensors, then any overrides.

        Called by the launcher once the sensor set is known; the constructor
        already does this without overrides so the engine is usable standalone.
        """
        for rule in (overrides or {}).values():
            self.alarms.set_rule(rule)
        self.alarms.configure(self._candidate_metrics(),
                              overrides=overrides or None)

    @property
    def alarm_metrics(self) -> list[str]:
        """Metrics that can carry an alarm on this machine."""
        return self._candidate_metrics()

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.manager.prime()
        self._last_tick_mono = time.monotonic()
        self._thread = threading.Thread(target=self._run, name="gpumon-sampler",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None
        if self.session_id is not None:
            self.stop_logging()
        self.writer.stop()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- logging ---------------------------------------------------------
    def start_logging(self, label: str = "", notes: str = "") -> int:
        with self._lock:
            if self.session_id is not None:
                return self.session_id
            caps = self.manager.capabilities()
            devices = [S.DeviceRecord(
                index=g.index, name=g.name, vendor=g.vendor,
                vram_total_mb=g.vram_total_mb, sources=list(g.sources),
                note=g.note, pci=g.pci, device_key=g.device_key)
                for g in caps.gpus]
            config = {
                "sample_hz": self.sample_hz,
                "per_core": self.per_core,
                "alarms": self.alarms.export(),
                "notes": notes,
            }
            started_wall = time.time()
            session_id = self.store.create_session(
                label=label, sample_hz=self.sample_hz, devices=devices,
                capabilities={"cpu_temp": caps.cpu_temp,
                              "backend_errors": caps.backend_errors,
                              "notes": caps.notes},
                config=config, app_version=APP_VERSION, started_at=started_wall)
            self.session_id = session_id
            self._label = label
            self._session_start_wall = started_wall
            # Align t=0 with the sample being taken right now, by anchoring on
            # that tick's timestamp (see the sampling loop).
            self._session_start_mono = (self._last_tick_mono
                                        or time.monotonic())
            self._samples_written = 0
            self.alarms.reset()
            self._recent_alarms.clear()
            self.writer.start()
            return session_id

    def stop_logging(self) -> SessionSummary | None:
        with self._lock:
            session_id = self.session_id
            if session_id is None:
                return None
            ended_wall = time.time()
            elapsed = max(0.0, time.monotonic() - self._session_start_mono)
            # Close any alarm still open so the timeline has an end time.
            for upd in self.alarms.force_close_all(elapsed, ended_wall):
                self._persist_alarm(session_id, upd)
                self._recent_alarms.append(upd)
            # Let the writer finish what it already holds, then make it durable.
            drained = self.writer.drain(timeout=30.0)
            self.store.flush()
            written = self.writer.written
            self.store.finish_session(session_id, ended_wall)
            self.session_id = None
            events = self.store.alarms(session_id)
            session = self.store.get_session(session_id)
            self._label = ""
            if not drained:
                self._last_error = (
                    f"disk fell behind: {self.writer.pending} samples still "
                    "queued when logging stopped")
            return SessionSummary(
                session_id=session_id, label=session.label if session else "",
                started_at=session.started_at if session else ended_wall,
                ended_at=ended_wall,
                duration=(session.duration if session else elapsed),
                samples=written, alarms=events,
                db_path=self.store.path)

    @property
    def logging(self) -> bool:
        return self.session_id is not None

    def logging_elapsed(self) -> float:
        if self.session_id is None:
            return 0.0
        return max(0.0, time.monotonic() - self._session_start_mono)

    # -- main loop -------------------------------------------------------
    def _run(self) -> None:
        self._priority_raised = raise_thread_priority()
        interval = 1.0 / self.sample_hz
        next_tick = time.monotonic()
        recent_ticks: collections.deque[float] = collections.deque(maxlen=30)
        while not self._stop.is_set():
            with self._lock:
                interval = 1.0 / self.sample_hz
            now = time.monotonic()
            # Detect a scheduling gap (system sleep / heavy preemption) rather
            # than pretending a long pause was a normal sample.
            if self._last_tick_mono and now - self._last_tick_mono > max(3.0, interval * 5):
                self._gap_count += 1
            self._last_tick_mono = now

            t0 = time.perf_counter()
            try:
                values = self.manager.poll()
            except Exception as exc:  # noqa: BLE001 - never kill the thread
                values = {}
                self._last_error = str(exc)
                if self.on_error:
                    self.on_error(str(exc))
            poll_ms = (time.perf_counter() - t0) * 1000.0
            wall = time.time()
            mono = time.monotonic()
            # Remember the tick's own timestamp: start_logging() anchors the
            # session clock on it, so t=0 is the sample the run began with. Using
            # a fresh reading there instead put the anchor *after* the poll, and
            # the first sample then counted as happening before the session
            # started - which showed up as alarm events with a start time of
            # -0.04s in the summary.
            self._last_tick_mono = mono

            with self._lock:
                session_id = self.session_id
                # Never negative: an alarm cannot start before its session, and
                # a negative start time reads as nonsense in the summary.
                session_t = (max(0.0, mono - self._session_start_mono)
                             if session_id is not None else 0.0)

            if values:
                self.live.append(mono, values)
                if self.on_sample:
                    self.on_sample(wall, values)
                for upd in self.alarms.evaluate(session_t, wall, values):
                    self._recent_alarms.append(upd)
                    if session_id is not None:
                        self._persist_alarm(session_id, upd)
                    if self.on_alarm:
                        self.on_alarm(upd)
                if session_id is not None:
                    # Hand the sample to the writer thread; never block here.
                    self.writer.submit(session_id, session_t, wall, values)
            else:
                self._dropped += 1

            self._poll_ms = poll_ms
            recent_ticks.append(mono)
            if len(recent_ticks) >= 2:
                span = recent_ticks[-1] - recent_ticks[0]
                if span > 0:
                    self._effective_hz = (len(recent_ticks) - 1) / span

            next_tick += interval
            sleep_for = next_tick - time.monotonic()
            if sleep_for < -interval * 2:
                # Fell far behind (e.g. a 400 ms poll at 5 Hz): resync instead
                # of spinning to catch up.
                next_tick = time.monotonic()
                sleep_for = 0.0
            if sleep_for > 0:
                self._stop.wait(sleep_for)

    def _persist_alarm(self, session_id: int, upd: A.AlarmUpdate) -> None:
        alarm = upd.alarm
        try:
            if upd.kind == "open":
                alarm.alarm_id = self.store.add_alarm(
                    session_id, metric=alarm.metric, level=alarm.level,
                    threshold=alarm.threshold, started_t=alarm.started_t,
                    started_wall=alarm.started_wall, peak=alarm.peak,
                    gpu_index=alarm.gpu_index, message=alarm.label)
            elif upd.kind == "close" and alarm.alarm_id is not None:
                self.store.update_alarm(
                    alarm.alarm_id, ended_t=upd.t, ended_wall=upd.wall,
                    peak=alarm.peak, samples=alarm.samples)
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"alarm persist failed: {exc}"

    # -- introspection ---------------------------------------------------
    def recent_alarms(self, limit: int = 50) -> list[A.AlarmUpdate]:
        return list(self._recent_alarms)[-limit:]

    def status(self) -> SamplerStatus:
        return SamplerStatus(
            sample_hz=self.sample_hz, effective_hz=self._effective_hz,
            poll_ms=self._poll_ms, dropped=self._dropped,
            last_error=self._last_error or self.writer.last_error,
            logging=self.logging, session_id=self.session_id,
            elapsed=self.logging_elapsed(),
            samples_written=self.writer.written,
            gap_count=self._gap_count)

    def queue_depth(self) -> int:
        """Samples captured but not yet on disk."""
        return self.writer.pending

    def dropped_samples(self) -> int:
        return self.writer.dropped
    def mark(self, label: str) -> None:
        """Drop a user marker into the running timeline (e.g. 'FurMark started')."""
        with self._lock:
            if self.session_id is None:
                return
            session_id = self.session_id
            session_t = max(0.0, time.monotonic() - self._session_start_mono)
        self.store.add_marker(session_id, session_t, time.time(), label)
