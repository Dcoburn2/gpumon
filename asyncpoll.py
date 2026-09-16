"""A tiny background poller for slow, latency-sensitive data sources.

Why this exists
---------------
Measured on the target workstation (an HP Z4 with GPU passthrough and a
"Balanced" power plan whose minimum processor state is 5%): a single NVML call
costs ~0.05-1.7 ms when idle but 16-116 ms with occasional 600 ms outliers when
every core is saturated, and PDH reads go from 15 ms to 11 s. Both amplify the
same way - the driver call has to wait for a CPU that has dropped into a deep
sleep state to wake up.

A monitoring tool must not lose its sample cadence to that, so GPU sources are
polled on their own thread and the sampler reads the latest snapshot. Values can
be a second or two stale under extreme load, which is a much better failure mode
than a sampler that stalls.

Only the latest snapshot is kept, so a slow source can never build a backlog.
"""
from __future__ import annotations

import threading
import time
from typing import Callable


class AsyncPoller:
    """Runs `fn` on a daemon thread and publishes its result under a lock."""

    def __init__(self, name: str, fn: Callable[[], dict[str, float]],
                 interval: float = 1.0, *, error_fn: Callable[[], str] | None = None) -> None:
        self.name = name
        self.interval = interval
        self._fn = fn
        self._error_fn = error_fn
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._start_lock = threading.Lock()
        self._data_lock = threading.Lock()
        self._data: dict[str, float] = {}
        self._stamp = 0.0
        self.error = ""
        self.polls = 0
        self.last_duration_ms = 0.0

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        with self._start_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name=self.name,
                                            daemon=True)
            self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
        self._thread = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # -- data ------------------------------------------------------------
    def snapshot(self) -> dict[str, float]:
        """Latest values. Returns {} until the first successful poll."""
        with self._data_lock:
            return dict(self._data)

    def age(self) -> float:
        """Seconds since the last successful poll (inf if never)."""
        with self._data_lock:
            if not self._stamp:
                return float("inf")
            return time.monotonic() - self._stamp

    def prime_once(self) -> None:
        """Synchronous single poll, for startup so the first sample is populated."""
        self._poll_and_publish()

    # -- internals -------------------------------------------------------
    def _poll_and_publish(self) -> None:
        t0 = time.perf_counter()
        try:
            data = self._fn()
        except Exception as exc:  # noqa: BLE001 - keep polling after a failure
            self.error = str(exc)
            return
        duration = (time.perf_counter() - t0) * 1000.0
        self.last_duration_ms = duration
        self.polls += 1
        with self._data_lock:
            self._data = data
            self._stamp = time.monotonic()
        self.error = self._error_fn() if self._error_fn else ""

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._poll_and_publish()
            # Subtract nothing: a poll slower than the interval simply runs
            # back-to-back, which is the desired behaviour for a slow source.
            self._stop.wait(self.interval)
