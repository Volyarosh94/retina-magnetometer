"""The shape every chart query returns, whether it came from memory or disk.

    {
      "t": [ms, ...],                 # sample time, or bucket start
      "n": [count, ...],              # samples behind each point; 0 marks a gap
      "x": {"min": [...], "mean": [...], "max": [...]},   # nT
      "y": {...}, "z": {...}, "b": {...},                 # b is |B|
      "source": "memory" | "samples" | "minutes",
      "bucket_ms": 0 for raw points, else the bucket width,
    }

A gap (a stretch with no samples) is a point whose values are all null, which
the chart draws as a break instead of a straight line across the outage.

Buckets sit on a grid of their own width, counted from the epoch rather than
from the start of the request. A window that slides forward therefore keeps
the same buckets: the chart does not shimmer as each refresh regroups the
samples, and a client holding the last response needs only the points from its
newest one on (``tail``). Values are rounded to 0.01 nT, far below one count
of the sensor (2.7 nT at its highest cycle count) and below its noise, which
halves the JSON.
"""

from __future__ import annotations

import bisect
import math

AXES = ("x", "y", "z", "b")
MAX_POINTS_CAP = 20_000
DECIMALS = 2


def clamp_points(max_points: int) -> int:
    return max(10, min(int(max_points), MAX_POINTS_CAP))


def bucket_width(start_ms: int, end_ms: int, max_points: int) -> int:
    """The bucket width that fits [start, end) into about ``max_points`` points."""
    return max(1, math.ceil(max(1, end_ms - start_ms) / max_points))


def bucketed_from(start_ms: int, bucket_ms: int, since_ms: int | None) -> int:
    """Where the samples to bucket begin: the window's start, or, for only
    what is new since ``since_ms``, the bucket before the one it falls in.
    The buckets before that are what the client already holds. That one is
    too, but a gap after it is marked one bucket on, which can be the very
    time ``since_ms`` asks from; the caller takes the tail."""
    if since_ms is None:
        return start_ms
    return max(start_ms, (since_ms // bucket_ms - 1) * bucket_ms)


def empty(source: str, bucket_ms: int) -> dict:
    out: dict = {"t": [], "n": [], "source": source, "bucket_ms": bucket_ms}
    for axis in AXES:
        out[axis] = {"min": [], "mean": [], "max": []}
    return out


def magnitude(x: float, y: float, z: float) -> float:
    return math.sqrt(x * x + y * y + z * z)


def gap_threshold_ms(times: list[int]) -> float:
    """Three typical sample intervals: a missing sample or two is not a gap,
    a sensor that stopped is."""
    if len(times) < 3:
        return math.inf
    steps = sorted(b - a for a, b in zip(times, times[1:]))
    return max(3 * steps[len(steps) // 2], 1)


def append_gap(out: dict, t: int) -> None:
    out["t"].append(t)
    out["n"].append(0)
    for axis in AXES:
        for key in ("min", "mean", "max"):
            out[axis][key].append(None)


def append_point(out: dict, t: int, n: int, values: tuple) -> None:
    """``values`` is (min, mean, max) for x, y, z and b, flattened: 12 numbers."""
    out["t"].append(t)
    out["n"].append(n)
    for i, axis in enumerate(AXES):
        out[axis]["min"].append(round(values[3 * i], DECIMALS))
        out[axis]["mean"].append(round(values[3 * i + 1], DECIMALS))
        out[axis]["max"].append(round(values[3 * i + 2], DECIMALS))


def raw_points(rows, source: str) -> dict:
    """Unaggregated (t, x, y, z) rows, in time order, with gaps marked."""
    out = empty(source, 0)
    gap = gap_threshold_ms([r[0] for r in rows])
    previous = None
    for t, x, y, z in rows:
        if previous is not None and t - previous > gap:
            append_gap(out, previous + 1)
        b = magnitude(x, y, z)
        append_point(out, t, 1, (x, x, x, y, y, y, z, z, z, b, b, b))
        previous = t
    return out


def bucketed_points(rows, bucket_ms: int, source: str) -> dict:
    """Rows of (k, t_first, n, 12 statistics) ordered by bucket index k, where
    bucket k starts at k * bucket_ms."""
    out = empty(source, bucket_ms)
    previous_k = None
    for row in rows:
        k = row[0]
        if previous_k is not None and k - previous_k > 2:
            append_gap(out, (previous_k + 1) * bucket_ms)
        append_point(out, k * bucket_ms, row[2], tuple(row[3:]))
        previous_k = k
    return out


def bucket_rows(
    rows, start_ms: int, end_ms: int, max_points: int, source: str = "memory", since_ms: int | None = None
) -> dict:
    """``Storage.series`` for rows already in memory (in time order): same
    shape, same rules, ``since_ms`` included."""
    rows = [r for r in rows if start_ms <= r[0] < end_ms]
    max_points = clamp_points(max_points)
    if len(rows) <= max_points:
        return raw_points(rows, source)
    bucket = bucket_width(start_ms, end_ms, max_points)
    first = bucketed_from(start_ms, bucket, since_ms)
    groups: dict[int, list] = {}
    for t, x, y, z in rows[bisect.bisect_left(rows, (first,)) :]:
        groups.setdefault(t // bucket, []).append((x, y, z, magnitude(x, y, z)))
    summarised = []
    for k in sorted(groups):
        members = groups[k]
        stats: list[float] = []
        for i in range(4):
            values = [m[i] for m in members]
            stats.extend((min(values), sum(values) / len(values), max(values)))
        summarised.append((k, None, len(members), *stats))
    return bucketed_points(summarised, bucket, source)


def tail(data: dict, since_ms: int) -> dict:
    """The points of ``data`` from ``since_ms`` on, everything else as it is.

    What a client holding an earlier response for the same span and points
    lacks, if ``since_ms`` is the time of its newest point: on a fixed grid,
    the buckets before it have not changed (that last one may have filled up,
    and comes again)."""
    first = bisect.bisect_left(data["t"], since_ms)
    out = {**data, "t": data["t"][first:], "n": data["n"][first:]}
    for axis in AXES:
        out[axis] = {key: values[first:] for key, values in data[axis].items()}
    return out
