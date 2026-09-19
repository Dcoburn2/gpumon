"""CPU telemetry published by gpumon's own elevated sensor helper.

This replaces reading LibreHardwareMonitor: the helper (sensorhelper.py) reads
the processor's registers through the PawnIO driver and writes what it found;
this turns that file into metric keys.

Staleness matters more than usual here. If the helper stops - it exits when the
program does, or when nobody has read it for a while - the file it left behind
still holds a perfectly good-looking temperature. Reporting that as the current
CPU temperature would be a lie told by an old number, so a reading older than a
few seconds is treated as no reading at all.
"""
from __future__ import annotations

import os
import time

import sensorhelper

#: A reading older than this is not a reading. The helper publishes once a
#: second, so this tolerates several missed polls before giving up.
MAX_AGE = 6.0

#: Metric keys the helper publishes that gpumon records.
KNOWN_KEYS = ("cpu_temp", "cpu_clock")


class MsrBackend:
    """Reads the helper's published sensors."""

    name = "msr"

    def __init__(self, path: str | None = None,
                 heartbeat: str | None = None) -> None:
        self.error = ""
        self.path = path or sensorhelper.sensors_path()
        #: The file a poll touches to say "somebody is still reading". Kept
        #: alongside the published file rather than looked up each time, so a
        #: caller can point both at a scratch directory - and so the pair cannot
        #: drift apart.
        self.heartbeat = heartbeat or sensorhelper.heartbeat_path()
        self._age = 0.0

    def _published_error(self, payload: dict) -> str:
        """The helper's own reason for having nothing to publish, if it gave one.

        A helper that cannot read the processor - no signed module for this CPU, a
        driver device it was not allowed to open - now says so in the file it
        publishes. Passing that on beats passing on silence, which reads as "the
        helper is not running" when the helper is running perfectly well.
        """
        message = payload.get("error")
        if isinstance(message, str) and message.strip():
            return f"the sensor helper cannot read the processor: {message}"
        return ""

    def available(self) -> bool:
        payload = sensorhelper.read_sensors(self.path)
        if not payload:
            self.error = "the sensor helper has not published anything yet"
            return False
        age = time.time() - float(payload.get("t") or 0.0)
        self._age = age
        if age > MAX_AGE:
            self.error = (f"the sensor helper stopped publishing "
                          f"({age:.0f}s ago)")
            return False
        self.error = self._published_error(payload)
        return not self.error

    def poll(self) -> dict[str, float]:
        payload = sensorhelper.read_sensors(self.path)
        if not payload:
            return {}
        age = time.time() - float(payload.get("t") or 0.0)
        self._age = age
        if age > MAX_AGE:
            self.error = (f"the sensor helper stopped publishing "
                          f"({age:.0f}s ago)")
            return {}
        # An error payload carries no numbers, so this comes back empty; the
        # reason is kept for whoever asks why nothing arrived.
        self.error = self._published_error(payload)
        out: dict[str, float] = {}
        for key, value in payload.items():
            if key == "t":
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out[key] = float(value)
        # A poll *is* a read, so it doubles as the helper's keep-alive: the helper
        # exits once nobody has asked for a reading for a while, and this is what
        # tells it somebody still is. That includes a helper that is failing: it
        # is kept alive so it can start working the moment the cause is fixed.
        sensorhelper.touch_heartbeat(self.heartbeat)
        return out

    def published_error(self) -> str:
        """Why the helper has no reading, for the interface to repeat."""
        return self._published_error(sensorhelper.read_sensors(self.path))

    def close(self) -> None:
        pass

    # -- lifetime helpers used by the program ---------------------------
    def reading_age(self) -> float:
        """Seconds since the last published reading, or a large number."""
        payload = sensorhelper.read_sensors(self.path)
        if not payload:
            return float("inf")
        return max(0.0, time.time() - float(payload.get("t") or 0.0))

    def stop(self) -> None:
        """Remove the published file, so nothing stale is left to be read."""
        for path in (self.path, self.heartbeat):
            try:
                os.remove(path)
            except OSError:
                pass
