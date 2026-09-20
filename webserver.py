"""HTTP/JSON server for the browser GUI.

Why hand-rolled
---------------
gpumon has no third-party dependencies and its target machines are often
air-gapped benchmark hosts, so this is `http.server` plus the standard library
and nothing else: no Flask, no bundled JS, no build step.

Design rules that matter
------------------------
* Nothing here polls hardware. The sampler thread already owns the sensors and
  the SQLite writer, and a handler that called `manager.poll()` would contend
  with it under exactly the CPU-saturated conditions this tool exists to
  measure. Every route reads state the sampler already produced - `sampler.live`
  for the live view, `Sampler.status()` for diagnostics, the alarm engine for
  the active set, or a read-only SQL query for history.
* Requests are read-only apart from the four control routes (logging start /
  stop, marker, theme). A browser on the loopback interface cannot corrupt a
  run.
* Nothing a client can send may kill the server: the dispatch loop catches
  everything and answers with a JSON error and a status code.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.parse
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import alarms as A
import metrics as M
import sampler as SP
import store as S
import webgui
from ui import themes

#: Loopback by default: the server has no authentication, so binding wider is
#: an explicit opt-in (`--host 0.0.0.0`) and is reported loudly at startup.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080

#: Control bodies are a label at most. Anything larger is a mistake or an
#: attempt to make the server allocate on the client's behalf.
MAX_BODY_BYTES = 64 * 1024

#: Cap for `?seconds=`/`?limit=` style parameters, so a hostile query cannot ask
#: for an unbounded scan.
MAX_HISTORY_SECONDS = float(SP.LIVE_WINDOW_SECONDS)
MAX_SESSION_LIMIT = 500


class HttpError(Exception):
    """A client-visible error carrying the status code to answer with."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------
# Payload builders
# --------------------------------------------------------------------------


def _gpu_dict(g: M.GpuDevice) -> dict[str, Any]:
    """One card as the browser sees it.

    `pci` and `luid_confidence` are included because two identical cards are
    otherwise indistinguishable in a session list; the browser prints the PCI
    address as the card's identity on the summary page.
    """
    return {
        "index": g.index,
        "name": g.name,
        "vendor": g.vendor,
        "pci": g.pci,
        "device_key": g.device_key,
        "vram_total_mb": g.vram_total_mb,
        "sources": list(g.sources),
        "note": g.note,
        "luid_confidence": g.luid_confidence,
        "temp_limit": g.temp_limit,
        "is_aggregate": g.is_aggregate,
        "full_telemetry": g.full_telemetry,
    }


def _device_record_dict(d: S.DeviceRecord) -> dict[str, Any]:
    return {"index": d.index, "name": d.name, "vendor": d.vendor, "pci": d.pci,
            "vram_total_mb": d.vram_total_mb, "sources": list(d.sources),
            "note": d.note, "device_key": d.device_key}


def _metric_dict(key: str) -> dict[str, Any]:
    d = M.metric_def(key)
    index = M.gpu_index_of(key)
    return {"key": key, "label": d.label, "unit": d.unit, "kind": d.kind,
            "precision": d.precision, "higher_is_worse": d.higher_is_worse,
            "gpu": index,
            # `gpu_field_of` partitions on the first underscore, so it is only
            # meaningful for gpuN_* keys; system metrics use their own key.
            "field": M.gpu_field_of(key) if index is not None else key}


#: Ordering mirrors the desktop cards (temperature first, then load, memory,
#: clocks, power) so the browser lists rows in the same order as the GUI.
_GPU_FIELD_ORDER = ["temp", "hotspot", "mem_temp", "util", "mem_util",
                    "vram_used", "vram_total", "vram_percent", "clock_core",
                    "clock_sm", "clock_mem", "power", "power_limit", "fan",
                    "power_percent", "throttle", "temp_limit"]
_SYSTEM_ORDER = ["cpu_util", "cpu_temp", "cpu_clock", "cpu_power", "ram_used",
                 "ram_total", "ram_percent", "pagefile_used"]


def metric_order(key: str) -> tuple[int, int, str]:
    index = M.gpu_index_of(key)
    if index is not None:
        field = M.gpu_field_of(key)
        pos = (_GPU_FIELD_ORDER.index(field) if field in _GPU_FIELD_ORDER
               else len(_GPU_FIELD_ORDER))
        return (0, index * 100 + pos, key)
    if key in _SYSTEM_ORDER:
        return (1, _SYSTEM_ORDER.index(key), key)
    if key.startswith("core"):
        return (2, 0, key)
    return (3, 0, key)


def _session_dict(s: S.SessionRecord) -> dict[str, Any]:
    return {"id": s.id, "label": s.label, "notes": s.notes,
            "started_at": s.started_at, "ended_at": s.ended_at,
            "duration": s.duration, "sample_hz": s.sample_hz,
            "sample_count": s.sample_count, "app_version": s.app_version,
            "devices": [_device_record_dict(d) for d in s.devices],
            "ongoing": s.ended_at is None}


def _stats_dict(st: S.MetricStats) -> dict[str, Any]:
    d = asdict(st)
    d["label"] = M.metric_def(st.metric).label
    d["unit"] = M.metric_def(st.metric).unit
    return d


def _alarm_dict(alarm: S.AlarmEvent) -> dict[str, Any]:
    return {"id": alarm.id, "metric": alarm.metric, "level": alarm.level,
            "threshold": alarm.threshold, "started_t": alarm.started_t,
            "ended_t": alarm.ended_t, "peak": alarm.peak,
            "samples": alarm.samples, "gpu_index": alarm.gpu_index,
            "message": alarm.message, "ongoing": alarm.ongoing,
            "duration": alarm.duration,
            "label": M.metric_def(alarm.metric).label}


def _active_alarm_dict(alarm: A.ActiveAlarm) -> dict[str, Any]:
    return {"metric": alarm.metric, "label": alarm.label, "level": alarm.level,
            "threshold": alarm.threshold, "peak": alarm.peak,
            "started_t": alarm.started_t, "started_wall": alarm.started_wall,
            "samples": alarm.samples, "pending": alarm.pending,
            "gpu_index": alarm.gpu_index}


def _param_float(query: dict[str, list[str]], name: str) -> float | None:
    raw = (query.get(name) or [""])[0].strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        raise HttpError(400, f"{name} must be a number") from None


def _param_int(query: dict[str, list[str]], name: str) -> int | None:
    raw = (query.get(name) or [""])[0].strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        raise HttpError(400, f"{name} must be an integer") from None


def _round_points(points: list[tuple[float, float]],
                  digits: int = 3) -> list[list[float]]:
    return [[round(t, digits), round(v, digits)] for t, v in points]


# --------------------------------------------------------------------------
# Server
# --------------------------------------------------------------------------


class WebServer:
    """Owns the listening socket and the read-only views of sampler state.

    Constructed the way `gpumon.launch_gui()` builds its stack, from the same
    `SensorManager` / `Store` / `Sampler` instances, so the browser sees exactly
    the numbers the desktop window would - including a run that is already
    logging.
    """

    def __init__(self, manager: M.SensorManager, store: S.Store, sampler: SP.Sampler,
                 *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                 verbose: bool = False, config_path: str | None = None) -> None:
        self.manager = manager
        self.store = store
        self.sampler = sampler
        self.host = host
        self.port = port
        self.verbose = verbose
        #: Where a theme switch is persisted. `None` means the user's real
        #: config.json; the tests pass their own path so that exercising
        #: POST /api/theme can never rewrite alarm rules the user owns.
        self.config_path = config_path
        #: Capabilities are probed once: `capabilities()` reaches for
        #: LibreHardwareMonitor over a named pipe, which is far too expensive to
        #: repeat per request.
        self.caps = manager.capabilities()
        self.requests = 0
        self.errors = 0
        self.started_at = time.time()

        self._seen: set[str] = set()
        self._catalogue: list[str] = []
        self._catalogue_seen = -1
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._attach()

    # -- lifecycle -------------------------------------------------------
    def _attach(self) -> None:
        """Learn which metric keys this machine really produces.

        The live buffer is indexed by metric but publishes no key list, and
        `metric_keys()` is a prediction about the hardware rather than a record
        of what arrived. The sampler callback runs on the sampler thread, so it
        must stay a set update - anything heavier would put work in the sampling
        path. An existing callback is chained rather than stolen.
        """
        previous = self.sampler.on_sample

        def on_sample(_wall: float, values: dict[str, float]) -> None:
            with self._lock:
                self._seen.update(values)
            if previous is not None:
                previous(_wall, values)

        self.sampler.on_sample = on_sample

    def metric_keys(self) -> list[str]:
        """Metric keys the browser may plot, in desktop display order.

        Recomputed only when a new key shows up in a sample, because
        `SensorManager.metric_keys()` re-probes the backends.
        """
        with self._lock:
            seen = len(self._seen)
            if self._catalogue and seen == self._catalogue_seen:
                return self._catalogue
            keys = set(self.manager.metric_keys())
            keys.update(self.sampler.alarm_metrics)
            keys.update(self._seen)
            self._catalogue = sorted(keys, key=metric_order)
            self._catalogue_seen = seen
            return self._catalogue

    def start(self) -> None:
        """Bind and serve on a background thread. Raises OSError if unbindable."""
        app = self

        class _Bound(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

            def __init__(self, address: tuple[str, int]) -> None:
                self.app = app
                super().__init__(address, _Handler)

        httpd = _Bound((self.host, self.port))
        # Port 0 means "pick one"; the tests rely on reading back the real port.
        self.port = int(httpd.server_address[1])
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever,
                                        name="gpumon-http", daemon=True,
                                        kwargs={"poll_interval": 0.2})
        self._thread.start()

    def wait(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def stop(self, timeout: float = 5.0) -> None:
        httpd, self._httpd = self._httpd, None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        self.wait(timeout)

    @property
    def url(self) -> str:
        host = self.host
        if ":" in host:                      # bare IPv6 literal
            host = f"[{host}]"
        return f"http://{host}:{self.port}/"

    @property
    def exposed(self) -> bool:
        """True when the bind address is reachable from other machines."""
        return self.host not in ("127.0.0.1", "localhost", "::1", "[::1]")

    def log(self, message: str) -> None:
        if self.verbose:
            stamp = time.strftime("%H:%M:%S")
            sys.stderr.write(f"[web {stamp}] {message}\n")

    # -- shared payloads -------------------------------------------------
    def status_payload(self) -> dict[str, Any]:
        st = self.sampler.status()
        return {
            "sample_hz": st.sample_hz,
            "effective_hz": round(st.effective_hz, 3),
            "poll_ms": round(st.poll_ms, 2),
            # `dropped` counts ticks with no data at all; `writer_dropped`
            # counts samples the disk could not keep up with. They are separate
            # failures and the footer shows both.
            "dropped": st.dropped,
            "writer_dropped": self.sampler.dropped_samples(),
            "queue_depth": self.sampler.queue_depth(),
            "last_error": st.last_error,
            "logging": st.logging,
            "session_id": st.session_id,
            "elapsed": st.elapsed,
            "samples_written": st.samples_written,
            "gap_count": st.gap_count,
            "uptime": time.time() - self.started_at,
        }

    def active_alarms(self) -> list[dict[str, Any]]:
        engine = self.sampler.alarms
        return [_active_alarm_dict(a) for a in engine.active.values()]

    def apply_theme(self, key: str) -> dict[str, Any]:
        """Switch the active palette and remember it in config.json.

        The switch happens first and unconditionally: it is what the page that
        is already open follows, and a config.json that cannot be written (a
        read-only checkout, a locked file) must not take the theme with it. The
        answer says whether the choice will survive a restart.
        """
        palette = themes.set_theme(key)
        config = A.load_config(self.config_path)
        # Read-modify-write on purpose: that file also holds the user's alarm
        # rules, and saving a fresh dict would silently delete them.
        config["theme"] = palette.key
        persisted = True
        try:
            A.save_config(config, self.config_path)
        except OSError as exc:
            persisted = False
            self.log(f"could not persist theme: {exc!r}")
        return {"ok": True, "theme": palette.key, "label": palette.label,
                "persisted": persisted}

    def default_session_id(self) -> int | None:
        """The session the summary opens with: the live one, else the newest."""
        if self.sampler.session_id is not None:
            return self.sampler.session_id
        sessions = self.store.list_sessions(limit=1)
        return sessions[0].id if sessions else None

    def _summary_alarm_window(self, alarms: list[S.AlarmEvent],
                              t0: float | None,
                              t1: float | None) -> list[S.AlarmEvent]:
        """Alarms overlapping [t0, t1]; an ongoing alarm counts as far-future."""
        if t0 is None and t1 is None:
            return alarms
        low = t0 if t0 is not None else float("-inf")
        high = t1 if t1 is not None else float("inf")
        return [a for a in alarms
                if a.started_t <= high
                and (a.ended_t if a.ended_t is not None else float("inf")) >= low]


class _Handler(BaseHTTPRequestHandler):
    """One request. Never raises out: every path answers JSON, even on error."""

    protocol_version = "HTTP/1.1"
    server_version = f"gpumon/{SP.APP_VERSION}"
    sys_version = ""
    #: A stalled client would otherwise hold a thread forever; the browser polls
    #: twice a second, so a 30 s stall means the tab is gone.
    timeout = 30.0

    # -- plumbing --------------------------------------------------------
    @property
    def app(self) -> WebServer:
        return self.server.app          # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        self.app.log(f"{self.address_string()} {fmt % args}")

    def do_GET(self) -> None:          # noqa: N802 - http.server's naming
        self._handle("GET")

    def do_POST(self) -> None:         # noqa: N802
        self._handle("POST")

    def do_HEAD(self) -> None:         # noqa: N802
        self._handle("GET")

    def _handle(self, method: str) -> None:
        self._responded = False
        app = self.app
        app.requests += 1
        try:
            parsed = urllib.parse.urlsplit(self.path)
            route = parsed.path.rstrip("/") or "/"
            query = urllib.parse.parse_qs(parsed.query)
            name = _ROUTES.get((method, route))
            if name is None:
                if route in _KNOWN_PATHS:
                    raise HttpError(405, f"{method} not allowed on {route}")
                raise HttpError(404, f"no such route: {parsed.path}")
            getattr(self, name)(query)
        except HttpError as exc:
            self._send_error(exc.status, str(exc))
        except Exception as exc:  # noqa: BLE001 - one bad request must not win
            app.errors += 1
            app.log(f"{method} {self.path} failed: {exc!r}")
            self._send_error(500, f"{type(exc).__name__}: {exc}")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise HttpError(400, "bad Content-Length header") from None
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise HttpError(413, "request body too large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except json.JSONDecodeError as exc:
            raise HttpError(400, f"invalid JSON body: {exc}") from None
        if not isinstance(payload, dict):
            raise HttpError(400, "body must be a JSON object")
        return payload

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        if self._responded:
            return
        self._responded = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # The page and the samples are regenerated per request; a cached copy
        # would silently hide new samples.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, allow_nan=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_error(self, status: int, message: str) -> None:
        try:
            self._json({"error": message, "status": status}, status)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- routes ----------------------------------------------------------
    def route_page(self, query: dict[str, list[str]]) -> None:
        self._send(200, webgui.page().encode("utf-8"),
                   "text/html; charset=utf-8")

    def route_favicon(self, query: dict[str, list[str]]) -> None:
        """Answer the browser's favicon probe with an empty icon.

        Without this every page load logs a 404 that means nothing.
        """
        self._send(204, b"", "image/x-icon")

    def route_state(self, query: dict[str, list[str]]) -> None:
        app = self.app
        palette = themes.current()
        self._json({
            "app_version": SP.APP_VERSION,
            # Which palette the CSS was rendered with, so the client can label
            # the picker and put it back if a switch is refused.
            "theme": palette.key,
            "theme_label": palette.label,
            "devices": [_gpu_dict(g) for g in app.caps.gpus],
            "capabilities": {
                "cpu_temp": app.caps.cpu_temp,
                "notes": list(app.caps.notes),
                "backend_errors": dict(app.caps.backend_errors),
            },
            "metrics": {key: _metric_dict(key) for key in app.metric_keys()},
            "status": app.status_payload(),
            "alarms": app.active_alarms(),
            "server": {"host": app.host, "port": app.port,
                       "exposed": app.exposed, "db": app.store.path,
                       "requests": app.requests, "errors": app.errors,
                       "uptime": time.time() - app.started_at},
        })

    def route_sample(self, query: dict[str, list[str]]) -> None:
        """The latest merged sample, straight out of the live buffer.

        This must stay cheap: the browser polls it twice a second and the
        sampler thread is holding the same lock that guards the buffer, so we
        read the last known value per metric instead of asking the hardware
        again.
        """
        app = self.app
        live = app.sampler.live
        values: dict[str, float] = {}
        for key in app.metric_keys():
            value = live.latest(key)
            if value is not None:
                values[key] = round(value, 4)
        span = live.span()
        wall = time.time()
        # Live-buffer timestamps are monotonic. Handing the browser the offset
        # lets it label a hovered sample with a real clock time.
        offset = (wall - span[1]) if span else 0.0
        self._json({
            "wall": wall,
            "t": span[1] if span else None,
            "wall_offset": offset,
            "values": values,
            "status": app.status_payload(),
            "active_alarms": app.active_alarms(),
        })

    def route_history(self, query: dict[str, list[str]]) -> None:
        app = self.app
        seconds = _param_float(query, "seconds")
        if seconds is None:
            seconds = 60.0
        seconds = max(1.0, min(MAX_HISTORY_SECONDS, seconds))
        only = (query.get("metric") or [""])[0].strip()
        keys = [only] if only else app.metric_keys()
        series: dict[str, list[list[float]]] = {}
        for key in keys:
            points = app.sampler.live.window(key, seconds)
            if points:
                series[key] = _round_points(points)
        self._json({"seconds": seconds, "series": series})

    def route_logging_start(self, query: dict[str, list[str]]) -> None:
        app = self.app
        payload = self._read_json()
        label = str(payload.get("label") or "").strip()[:200]
        already = app.sampler.logging
        session_id = app.sampler.start_logging(label)
        self._json({"session_id": session_id, "label": label,
                    "logging": True, "already_logging": already})

    def route_logging_stop(self, query: dict[str, list[str]]) -> None:
        app = self.app
        summary = app.sampler.stop_logging()
        if summary is None:
            self._json({"session_id": None, "logging": False, "stopped": False})
            return
        self._json({
            "session_id": summary.session_id, "stopped": True, "logging": False,
            "label": summary.label, "samples": summary.samples,
            "duration": summary.duration, "alarms": len(summary.alarms),
            "started_at": summary.started_at, "ended_at": summary.ended_at,
        })

    def route_marker(self, query: dict[str, list[str]]) -> None:
        app = self.app
        payload = self._read_json()
        label = str(payload.get("label") or "").strip()[:200]
        # `Sampler.mark` is a no-op outside a run rather than an error, so the
        # answer reports whether it landed instead of pretending it did.
        logging = app.sampler.logging
        if logging:
            app.sampler.mark(label or "mark")
        self._json({"ok": logging, "label": label,
                    "session_id": app.sampler.session_id})

    def route_theme(self, query: dict[str, list[str]]) -> None:
        """Switch the colour theme and persist it.

        The body is `{"theme": "<name>"}`. Names are normalized rather than
        matched literally, so the spellings `--theme` accepts ("toji",
        "gruvbox-dark") work here too; an unknown one is a 400 instead of the
        silent fall-back `themes.set_theme` performs, because a typo the user
        cannot see is worse than an error they can.
        """
        app = self.app
        payload = self._read_json()
        raw = str(payload.get("theme") or "").strip()
        key = themes.normalize(raw)
        if not key:
            raise HttpError(400, f"unknown theme: {raw!r}. Known themes: "
                                 f"{', '.join(themes.keys())}")
        self._json(app.apply_theme(key))

    def route_sessions(self, query: dict[str, list[str]]) -> None:
        app = self.app
        limit = _param_int(query, "limit")
        if limit is None:
            limit = 50
        limit = max(1, min(MAX_SESSION_LIMIT, limit))
        out = []
        for record in app.store.list_sessions(limit=limit):
            item = _session_dict(record)
            item["alarm_count"] = len(app.store.alarms(record.id))
            out.append(item)
        self._json({"sessions": out, "limit": limit,
                    "live_session_id": app.sampler.session_id})

    def route_summary(self, query: dict[str, list[str]]) -> None:
        """Statistics for one session, optionally limited to [t0, t1].

        The browser sends the dragged window here, so `t0`/`t1` are honoured as
        given and echoed back; the renderer uses them to shade the selection.
        """
        app = self.app
        session_id = _param_int(query, "session_id")
        if session_id is None:
            session_id = app.default_session_id()
        if session_id is None:
            raise HttpError(404, "no sessions logged yet")
        session = app.store.get_session(session_id)
        if session is None:
            raise HttpError(404, f"session {session_id} not found")
        t0 = _param_float(query, "t0")
        t1 = _param_float(query, "t1")
        if t0 is not None and t1 is not None and t1 < t0:
            t0, t1 = t1, t0
        span = app.store.sample_span(session_id)
        stats = app.store.stats_many(session_id, t0, t1)
        alarms = app._summary_alarm_window(app.store.alarms(session_id), t0, t1)
        markers = [(t, wall, label, kind)
                   for t, wall, label, kind in app.store.markers(session_id)
                   if (t0 is None or t >= t0) and (t1 is None or t <= t1)]
        self._json({
            "session": _session_dict(session),
            "window": {"t0": t0, "t1": t1,
                       "span": list(span) if span else None},
            "stats": {key: _stats_dict(st) for key, st in stats.items()},
            "metrics": app.store.metrics_for_session(session_id),
            "alarms": [_alarm_dict(a) for a in alarms],
            "markers": [{"t": t, "wall": wall, "label": label, "kind": kind}
                        for t, wall, label, kind in markers],
            "devices": [_device_record_dict(d) for d in session.devices],
            "capabilities": session.capabilities,
            "notes": session.notes,
        })

    def route_series(self, query: dict[str, list[str]]) -> None:
        app = self.app
        session_id = _param_int(query, "session_id")
        if session_id is None:
            session_id = app.default_session_id()
        if session_id is None:
            raise HttpError(404, "no sessions logged yet")
        if app.store.get_session(session_id) is None:
            raise HttpError(404, f"session {session_id} not found")
        metric = (query.get("metric") or [""])[0].strip()
        if not metric:
            raise HttpError(400, "metric is required")
        if metric not in app.store.metrics_for_session(session_id):
            raise HttpError(404, f"metric {metric} is not recorded in session "
                                 f"{session_id}")
        t0 = _param_float(query, "t0")
        t1 = _param_float(query, "t1")
        points = app.store.series(session_id, metric, t0, t1)
        info = _metric_dict(metric)
        self._json({"session_id": session_id, "metric": metric,
                    "label": info["label"], "unit": info["unit"],
                    "precision": info["precision"], "kind": info["kind"],
                    "window": {"t0": t0, "t1": t1},
                    "series": _round_points(points)})


#: Route table: method + path -> handler method name on `_Handler`. Keeping it
#: declarative is what makes the 404 / 405 split above a two-line lookup.
_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/"): "route_page",
    ("GET", "/favicon.ico"): "route_favicon",
    ("GET", "/api/state"): "route_state",
    ("GET", "/api/sample"): "route_sample",
    ("GET", "/api/history"): "route_history",
    ("GET", "/api/sessions"): "route_sessions",
    ("GET", "/api/summary"): "route_summary",
    ("GET", "/api/series"): "route_series",
    ("POST", "/api/logging/start"): "route_logging_start",
    ("POST", "/api/logging/stop"): "route_logging_stop",
    ("POST", "/api/marker"): "route_marker",
    ("POST", "/api/theme"): "route_theme",
}

_KNOWN_PATHS = {path for _method, path in _ROUTES}


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def create_server(manager: M.SensorManager, store: S.Store, sampler: SP.Sampler,
                  *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  verbose: bool = False,
                  config_path: str | None = None) -> WebServer:
    """Build a bound `WebServer`. The tests use this with `port=0`."""
    return WebServer(manager, store, sampler, host=host, port=port,
                     verbose=verbose, config_path=config_path)


def _wants_verbose(args: argparse.Namespace, extra: list[str] | None) -> bool:
    """`--verbose` lives outside gpumon's parser, which passes it through."""
    if bool(getattr(args, "verbose", False)):
        return True
    return any(flag in ("--verbose", "-v") for flag in (extra or []))


def serve(args: argparse.Namespace, extra: list[str] | None = None) -> int:
    """Run the browser GUI until interrupted. Returns a process exit code.

    Called from `gpumon.main()` before the database and rate defaults are
    resolved, so the same fallbacks are applied here: explicit flag, then
    config.json, then the built-in default.
    """
    config = A.load_config()
    db_path = (getattr(args, "db", None) or config.get("db_path")
               or S.default_db_path())
    hz = getattr(args, "hz", None)
    hz = float(hz) if hz is not None else float(config.get("sample_hz", 1.0))
    per_core = (bool(getattr(args, "per_core", False))
                or bool(config.get("per_core", False)))
    host = getattr(args, "host", None) or DEFAULT_HOST
    port = getattr(args, "port", None)
    port = DEFAULT_PORT if port is None else int(port)
    verbose = _wants_verbose(args, extra)

    store = S.Store(db_path)
    manager = M.SensorManager(per_core=per_core)
    engine = A.AlarmEngine.from_config(config.get("alarms"))
    sampler = SP.Sampler(manager, store, sample_hz=hz, alarm_engine=engine,
                         per_core=per_core)
    # Same as launch_gui(): defaults for the detected sensors, then config.
    sampler.configure_alarms(engine.rules or None)
    web = create_server(manager, store, sampler, host=host, port=port,
                        verbose=verbose)
    try:
        web.start()
    except OSError as exc:
        print(f"gpumon: cannot bind {host}:{port} - {exc}")
        manager.close()
        store.close()
        return 1

    sampler.start()
    print("=" * 74)
    print("gpumon web GUI")
    print("=" * 74)
    print(f"  url     {web.url}")
    print(f"  db      {store.path}")
    print(f"  devices {len(manager.gpus)}  hz {sampler.sample_hz}"
          f"{'  per-core' if per_core else ''}")
    if web.exposed:
        # No authentication exists, so a non-loopback bind is worth shouting
        # about: anyone who can reach the port can start and stop logging.
        print(f"  WARNING bound to {host}: reachable from the network and "
              "unauthenticated")
    print("  Ctrl-C to stop")
    print("=" * 74)
    try:
        web.wait()
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        web.stop()
        sampler.stop()
        manager.close()
        store.close()
    return 0
