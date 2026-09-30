"""The browser UI and its JSON API.

One page (``/``) and a handful of read-only endpoints. Nothing here changes
the sensor or the stored data: configuration lives in the service's
environment, where the node's other services keep theirs, and an
unauthenticated LAN port is the wrong place for a write surface.

    GET /api/series       chart data for a window (``?window=<s>``) or a range
                          (``?start=<ms>&end=<ms>``), at most ``?points=`` points
    GET /api/latest       the newest sample
    GET /api/health       the health snapshot (the same document as status.json)
    GET /api/orientation  the mounting worked out from the field (orientation.py)
    GET /api/config       the effective configuration, location and sessions
    GET /healthz          "ok" while the process serves requests
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from flask import Blueprint, Flask, abort, current_app, jsonify, render_template, request

from retina_magnetometer import orientation, series
from retina_magnetometer.config import Config
from retina_magnetometer.health import Health
from retina_magnetometer.location import Location
from retina_magnetometer.recorder import Recorder
from retina_magnetometer.storage import Storage

VERSION = "0.1.0"

# The windows the page offers, in seconds. Anything in between is accepted.
WINDOWS = (600, 3600, 6 * 3600, 86_400, 7 * 86_400, 30 * 86_400)
MAX_WINDOW_S = 400 * 86_400
DEFAULT_POINTS = 1500

bp = Blueprint("ui", __name__)


@dataclass
class Services:
    config: Config
    health: Health
    recorder: Recorder
    storage: Storage
    location: Location | None
    clock: object = time.time


def _services() -> Services:
    return current_app.extensions["magnetometer"]


def create_app(services: Services) -> Flask:
    app = Flask(__name__)
    app.extensions["magnetometer"] = services
    app.register_blueprint(bp)
    return app


def _int_arg(name: str, default: int | None = None) -> int | None:
    value = request.args.get(name)
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except ValueError:
        abort(400, description=f"{name} must be a number")


@bp.get("/")
def index():
    s = _services()
    return render_template("index.html", version=VERSION, windows=WINDOWS, config=s.config)


@bp.get("/healthz")
def healthz():
    return "ok\n", 200, {"Content-Type": "text/plain"}


@bp.get("/api/health")
def api_health():
    return jsonify(_services().health.snapshot())


@bp.get("/api/latest")
def api_latest():
    snapshot = _services().health.snapshot()
    return jsonify(
        {"at": snapshot["last_sample_at"], "age_s": snapshot["last_sample_age_s"], "sample": snapshot["last_sample"]}
    )


@bp.get("/api/series")
def api_series():
    s = _services()
    now_ms = int(s.clock() * 1000)
    points = series.clamp_points(_int_arg("points", DEFAULT_POINTS))
    start = _int_arg("start")
    end = _int_arg("end")
    if start is None or end is None:
        window = _int_arg("window", 600)
        if not 10 <= window <= MAX_WINDOW_S:
            abort(400, description=f"window must be 10..{MAX_WINDOW_S} seconds")
        end = now_ms
        start = end - window * 1000
    if end <= start or end - start > MAX_WINDOW_S * 1000:
        abort(400, description="need start < end, at most 400 days apart")
    data = _series(s, start, end, points)
    data.update({"start": start, "end": end, "now": now_ms})
    return jsonify(data)


# Raw rows fetched from disk to join with the in-memory buffer, at most. Past
# this the range is long enough that the buffer's few unflushed seconds are
# invisible, and the disk's own aggregation is far cheaper.
MERGE_ROW_LIMIT = 200_000


def _series(s: Services, start: int, end: int, points: int) -> dict:
    """Chart data for [start, end], from wherever it is freshest and cheapest.

    The in-memory buffer holds the last hour, including the seconds not yet
    flushed; the database holds everything else. A window inside the buffer
    comes from it alone. A short window reaching back past it joins the disk's
    raw rows onto the buffer, so a page opened just after a restart (or with a
    long flush interval) still runs up to the latest sample. Windows whose
    points are a minute or more wide come from the minute summaries.
    """
    covered_from = s.recorder.recent_coverage_ms()
    if covered_from is None:
        return s.storage.series(start, end, points)
    if start >= covered_from:
        return series.bucket_rows(s.recorder.recent(start), start, end, points)
    if (end - start) / points >= 60_000:
        return s.storage.series(start, end, points)
    older = s.storage.raw_rows(start, covered_from, MERGE_ROW_LIMIT)
    if older is None:
        return s.storage.series(start, end, points)
    return series.bucket_rows(older + s.recorder.recent(covered_from), start, end, points, source="samples+memory")


@bp.get("/api/orientation")
def api_orientation():
    s = _services()
    now = s.clock()
    rows = s.recorder.recent(int((now - s.config.orientation_window_s) * 1000))
    reference = None
    if s.location is not None:
        reference = orientation.reference_field(s.location, datetime.fromtimestamp(now, tz=timezone.utc))
    if not rows:
        result = orientation.estimate((0.0, 0.0, 0.0), 0, reference)
    else:
        # The median, not the mean: a passing object or a spike in the window
        # should not move the answer.
        mean = (
            statistics.median(r[1] for r in rows),
            statistics.median(r[2] for r in rows),
            statistics.median(r[3] for r in rows),
        )
        result = orientation.estimate(mean, len(rows), reference)
    payload = result.as_dict()
    payload["window_s"] = s.config.orientation_window_s
    payload["location"] = (
        None
        if s.location is None
        else {
            "latitude": s.location.latitude,
            "longitude": s.location.longitude,
            "altitude_m": s.location.altitude_m,
            "source": s.location.source,
        }
    )
    return jsonify(payload)


@bp.get("/api/config")
def api_config():
    s = _services()
    return jsonify(
        {
            "version": VERSION,
            "config": s.config.public(),
            "location": None
            if s.location is None
            else {
                "latitude": s.location.latitude,
                "longitude": s.location.longitude,
                "altitude_m": s.location.altitude_m,
                "source": s.location.source,
            },
            "sessions": s.storage.sessions(5),
        }
    )


@bp.app_errorhandler(400)
def bad_request(error):
    return jsonify({"error": getattr(error, "description", "bad request")}), 400
