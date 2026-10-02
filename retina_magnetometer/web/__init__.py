"""The browser UI and its JSON API.

One page (``/``) and a handful of read-only endpoints. Nothing here changes
the sensor or the stored data: configuration lives in the service's
environment, where the node's other services keep theirs, and an
unauthenticated LAN port is the wrong place for a write surface.

    GET /api/series       chart data for a window (``?window=<s>``) or a range
                          (``?start=<ms>&end=<ms>``), at most ``?points=`` points;
                          ``?since=<ms>`` returns only the points from then on
    GET /api/latest       the newest sample
    GET /api/health       the health snapshot (the same document as status.json)
    GET /api/orientation  the mounting worked out from the field (orientation.py)
    GET /api/config       the effective configuration, location and sessions
    GET /healthz          "ok" while the process serves requests

JSON responses of a kilobyte or more are gzipped for clients that accept it.
"""

from __future__ import annotations

import gzip
import math
import sqlite3
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

# Smaller responses are not worth the CPU; chart data compress about tenfold.
GZIP_MIN_BYTES = 1024

# The times a query may name, in ms: from 1970 to the year 3000. Anything else
# is a mistake, and past 2^63 it does not even fit an SQLite integer.
LATEST_MS = 32_503_680_000_000

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
        number = float(value)
    except ValueError:
        number = math.nan
    if not math.isfinite(number):
        abort(400, description=f"{name} must be a number")
    return int(number)


def _time_arg(name: str) -> int | None:
    """A time in ms since the epoch, between 1970 and the year 3000."""
    value = _int_arg(name)
    if value is not None and not 0 <= value <= LATEST_MS:
        abort(400, description=f"{name} must be a time in ms between 0 and {LATEST_MS} (the year 3000)")
    return value


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
    start = _time_arg("start")
    end = _time_arg("end")
    since = _time_arg("since")
    if start is None or end is None:
        window = _int_arg("window", 600)
        if not 10 <= window <= MAX_WINDOW_S:
            abort(400, description=f"window must be 10..{MAX_WINDOW_S} seconds")
        end = now_ms
        start = end - window * 1000
    if end <= start or end - start > MAX_WINDOW_S * 1000:
        abort(400, description="need start < end, at most 400 days apart")
    try:
        data = _series(s, start, end, points, since)
    except (sqlite3.Error, OSError) as exc:
        # The database cannot be read (the storage card says why); the live
        # buffer still can, so the chart shows what it holds.
        data = series.bucket_rows(s.recorder.recent(start), start, end, points, since_ms=since)
        data["warning"] = f"Stored history unavailable ({exc}); showing the live buffer only."
    if since is not None:
        # Everything before ``since`` is what the client already holds. The
        # buckets are those of the whole window, worked out only from the one
        # ``since`` is in, so they and their gaps agree with a full answer.
        data = series.tail(data, since)
        data["since"] = since
    data.update({"start": start, "end": end, "now": now_ms})
    return jsonify(data)


# Raw rows fetched from disk to join with the in-memory buffer, at most. Past
# this the range is long enough that the buffer's few unflushed seconds are
# invisible, and the disk's own aggregation is far cheaper.
MERGE_ROW_LIMIT = 200_000


def _series(s: Services, start: int, end: int, points: int, since: int | None = None) -> dict:
    """Chart data for [start, end], from wherever it is freshest and cheapest
    (from the bucket ``since`` falls in on, if given).

    The in-memory buffer holds the last hour, including the seconds not yet
    flushed; the database holds everything else. A range inside the buffer
    comes from it alone, and one that ends before it from the disk alone. A
    short range reaching back past it joins the disk's raw rows onto the
    buffer, so a page opened just after a restart (or with a long flush
    interval) still runs up to the latest sample. Ranges whose points are a
    minute or more wide come from the minute summaries.
    """
    covered_from = s.recorder.recent_coverage_ms()
    if covered_from is None:
        return s.storage.series(start, end, points, since)
    if start >= covered_from:
        return series.bucket_rows(s.recorder.recent(start), start, end, points, since_ms=since)
    if end <= covered_from or (end - start) / points >= 60_000:
        return s.storage.series(start, end, points, since)
    older = s.storage.raw_rows(start, covered_from, MERGE_ROW_LIMIT)
    if older is None:
        return s.storage.series(start, end, points, since)
    rows = older + s.recorder.recent(covered_from)
    return series.bucket_rows(rows, start, end, points, source="samples+memory", since_ms=since)


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
    try:
        sessions = s.storage.sessions(5)
    except (sqlite3.Error, OSError):
        sessions = []  # the storage card says why
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
            "sessions": sessions,
        }
    )


@bp.app_errorhandler(400)
def bad_request(error):
    return jsonify({"error": getattr(error, "description", "bad request")}), 400


@bp.after_app_request
def compress(response):
    """gzip JSON for clients that accept it: the live chart refetches every
    two seconds, and a node may be reached over a slow link."""
    if response.mimetype != "application/json" or response.direct_passthrough:
        return response
    response.vary.add("Accept-Encoding")
    if "Content-Encoding" in response.headers or not request.accept_encodings["gzip"]:
        return response
    body = response.get_data()
    if len(body) >= GZIP_MIN_BYTES:
        response.set_data(gzip.compress(body, compresslevel=6))
        response.headers["Content-Encoding"] = "gzip"
    return response
