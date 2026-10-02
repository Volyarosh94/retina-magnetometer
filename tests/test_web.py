"""The JSON API and the page, through Flask's test client."""

import gzip
import json
import math
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from retina_magnetometer import web
from retina_magnetometer.config import from_env
from retina_magnetometer.health import Health
from retina_magnetometer.location import Location
from retina_magnetometer.recorder import Recorder
from retina_magnetometer.storage import Storage
from retina_magnetometer.web import Services, create_app
from rm3100_sim import physics

NOW = 1_790_769_600.0  # 2026-09-30T12:00:00Z


@pytest.fixture
def rig(tmp_path):
    config = from_env(
        {
            "MAGNETOMETER_DATA_DIR": str(tmp_path),
            "MAGNETOMETER_LATITUDE": "34.85",
            "MAGNETOMETER_LONGITUDE": "-82.39",
            "MAGNETOMETER_ALTITUDE_M": "300",
        }
    )
    health = Health(config, clock=lambda: NOW)
    storage = Storage(config.db_path, raw_retention_days=7, rollup_retention_days=365, max_db_mb=64, clock=lambda: NOW)
    recorder = Recorder(storage, health, config, clock=lambda: NOW)
    location = Location(34.85, -82.39, 300.0, "environment")
    app = create_app(
        Services(config=config, health=health, recorder=recorder, storage=storage, location=location, clock=lambda: NOW)
    )
    # Ten minutes of a sensor mounted like the demo: +X 37° east of true
    # north, upside down.
    ref = physics.reference_field(physics.Site(34.85, -82.39, 300.0), NOW)
    field = physics.ned_to_sensor(physics.rotation_ned_from_sensor(37.0, 0.0, 180.0), ref.vector)
    for i in range(600):
        t = NOW - 600 + i
        wobble = 20 * math.sin(i / 30)
        recorder.add(int(t * 1000), field[0] + wobble, field[1], field[2])
        health.sample(t, field[0] + wobble, field[1], field[2])
    return app.test_client(), recorder, storage, health


def test_page_renders(rig):
    client, *_ = rig
    page = client.get("/")
    assert page.status_code == 200
    html = page.get_data(as_text=True)
    assert "RM3100 magnetometer" in html and "plotly-basic-3.7.0.min.js" in html
    assert client.get("/static/vendor/plotly-basic-3.7.0.min.js").status_code == 200


def test_healthz(rig):
    client, *_ = rig
    assert client.get("/healthz").get_data(as_text=True) == "ok\n"


def test_series_from_memory_at_the_live_edge(rig):
    client, *_ = rig
    data = client.get("/api/series?window=600&points=2000").get_json()
    assert data["source"] == "memory"
    assert len(data["t"]) == 600
    assert data["end"] - data["start"] == 600_000


def test_series_from_disk_further_back(rig):
    client, recorder, storage, _ = rig
    recorder.flush()
    url = f"/api/series?start={int((NOW - 7200) * 1000)}&end={int(NOW * 1000)}"
    data = client.get(url + "&points=400").get_json()
    # Buckets under a minute: raw samples, the disk's joined onto the buffer's.
    assert data["source"] == "samples+memory"
    assert data["bucket_ms"] == 18_000
    assert sum(data["n"]) == 600
    storage.rollup(now_ms=int(NOW * 1000) + 60_000)
    data = client.get(url + "&points=100").get_json()
    assert data["source"] == "minutes"  # buckets over a minute: from the summaries
    assert data["bucket_ms"] == 120_000
    assert sum(data["n"]) == 600


@pytest.mark.parametrize(
    "query",
    [
        "window=abc",
        "window=5",
        "window=999999999",
        "start=10&end=5",
        "start=0&end=99999999999999",
        # Not finite: int(float()) of these used to escape as a 500.
        "window=inf",
        "window=-inf",
        "window=nan",
        "points=inf",
        "points=1e400",
        "start=inf&end=1",
        "start=1&end=-inf",
        "since=nan",
        # Finite, but past what SQLite stores as an integer: these were 500s.
        "start=-10000000000000000000&end=-9999999999999997952",
        "start=10000000000000000000&end=10000000000000002048",
        "window=600&since=1e300",
        "window=600&since=-1",
        "start=-1&end=1000",
        "start=1&end=32503680000001",
    ],
)
def test_series_rejects_nonsense(rig, query):
    client, *_ = rig
    response = client.get(f"/api/series?{query}")
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_health_is_the_status_document(rig):
    client, *_ = rig
    h = client.get("/api/health").get_json()
    assert h["schema"] == 1 and h["state"] == "ok"
    assert h["samples_total"] == 600
    assert h["last_sample_age_s"] == pytest.approx(1.0)


def test_latest(rig):
    client, *_ = rig
    latest = client.get("/api/latest").get_json()
    assert latest["sample"]["b"] == pytest.approx(48_600, abs=100)


def test_orientation_recovers_the_mounting(rig):
    client, *_ = rig
    o = client.get("/api/orientation").get_json()
    assert o["down_axis"] == "-Z" and o["up_axis"] == "+Z"
    assert abs(o["heading_true_deg"] - 37.0) < 0.5
    assert o["verdict"] == "good"
    assert o["location"]["source"] == "environment"
    assert o["reference"]["model"] == "WMM2025"


def test_config_endpoint(rig):
    client, _, storage, _ = rig
    storage.start_session(cycle_count=200, gain=74.92, rate_hz=1.0, mode="poll", bus="test", address=0x20)
    c = client.get("/api/config").get_json()
    assert c["config"]["port"] == 3030
    assert c["location"]["latitude"] == 34.85
    assert c["sessions"][0]["cycle_count"] == 200


def test_status_file_is_written_atomically(rig, tmp_path):
    _, recorder, _, health = rig
    recorder._write_status()
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["state"] == "ok" and status["schema"] == 1
    assert oct((tmp_path / "status.json").stat().st_mode & 0o777) == "0o644"
    assert not (tmp_path / "status.json.tmp").exists()


def test_series_joins_disk_and_memory_across_the_buffer_edge(rig):
    client, recorder, storage, _ = rig
    # An hour of older samples on disk only, then the live buffer's ten minutes.
    older = [(int((NOW - 4200 + i) * 1000), 1.0, 2.0, 3.0) for i in range(3600)]
    storage.write_samples(older)
    data = client.get("/api/series?window=5400&points=20000").get_json()
    assert data["source"] == "samples+memory"
    assert sum(data["n"]) == 3600 + 600
    assert data["t"][-1] == int((NOW - 1) * 1000)  # up to the newest, unflushed sample


def test_a_range_before_the_buffer_reads_only_that_range(rig, monkeypatch):
    # A short zoom into older history comes from the disk alone, aggregated by
    # SQLite; joining it to the buffer used to read every row up to the
    # buffer's edge first.
    client, recorder, storage, _ = rig
    storage.write_samples([(int((NOW - 50 * 3600 + i) * 1000), 1.0, 2.0, 3.0) for i in range(3600)])

    def unbounded(*args):
        raise AssertionError("raw_rows reads up to the buffer's edge")

    monkeypatch.setattr(storage, "raw_rows", unbounded)
    start = int((NOW - 50 * 3600 + 600) * 1000)
    data = client.get(f"/api/series?start={start}&end={start + 600_000}&points=2000").get_json()
    assert data["source"] == "samples" and len(data["t"]) == 600


def test_series_since_returns_what_a_client_lacks(rig):
    # The live chart holds the last response and asks only for what follows
    # its newest point; buckets on a fixed grid make the rest identical.
    client, recorder, *_ = rig
    full = client.get("/api/series?window=600&points=100").get_json()
    assert full["bucket_ms"] == 6000
    newest = full["t"][-1]
    tail = client.get(f"/api/series?window=600&points=100&since={newest}").get_json()
    assert tail["since"] == newest and tail["t"] == [newest]
    assert tail["bucket_ms"] == full["bucket_ms"] and tail["start"] == full["start"]
    for axis in ("x", "y", "z", "b"):
        assert tail[axis]["mean"] == full[axis]["mean"][-1:]


@pytest.mark.parametrize(
    "query,source",
    [
        ("window=600&points=100", "memory"),
        ("window=3600&points=1800", "samples+memory"),
        (f"start={int((NOW - 3000) * 1000)}&end={int((NOW - 1200) * 1000)}&points=100", "samples"),
        ("window=86400&points=1000", "minutes"),
    ],
)
def test_series_since_is_the_tail_of_the_whole_answer_from_every_source(rig, query, source):
    # Worked out only from the bucket ``since`` is in, wherever the series
    # comes from, the answer is exactly the end of the full one.
    client, recorder, storage, _ = rig
    older = [
        (int((NOW - 3600 + i) * 1000), 22_400.0 + 30 * math.sin(i / 90), -2_760.0 + i % 7, 43_000.0)
        for i in range(3000)
    ]
    storage.write_samples(older)
    storage.rollup(now_ms=int((NOW - 600) * 1000))
    full = client.get(f"/api/series?{query}").get_json()
    assert full["source"] == source and len(full["t"]) > 4
    middle = full["t"][len(full["t"]) // 2]
    for since in (full["t"][-1], middle, middle + 1, full["start"] - 1):
        tail = client.get(f"/api/series?{query}&since={since}").get_json()
        first = next(i for i, t in enumerate(full["t"]) if t >= since)
        assert tail["since"] == since and tail["source"] == source and tail["bucket_ms"] == full["bucket_ms"]
        assert tail["t"] == full["t"][first:] and tail["n"] == full["n"][first:]
        for axis in ("x", "y", "z", "b"):
            for part in ("min", "mean", "max"):
                assert tail[axis][part] == full[axis][part][first:]


def test_large_responses_are_gzipped_for_clients_that_accept_it(rig):
    client, *_ = rig
    plain = client.get("/api/series?window=600&points=2000")
    assert "Content-Encoding" not in plain.headers and "Accept-Encoding" in plain.headers["Vary"]
    packed = client.get("/api/series?window=600&points=2000", headers={"Accept-Encoding": "gzip, br"})
    assert packed.headers["Content-Encoding"] == "gzip"
    body = packed.get_data()
    assert int(packed.headers["Content-Length"]) == len(body) < len(plain.get_data()) / 4
    assert json.loads(gzip.decompress(body)) == plain.get_json()
    small = client.get("/api/latest", headers={"Accept-Encoding": "gzip"})
    assert "Content-Encoding" not in small.headers  # not worth it under a kilobyte
    assert client.get("/", headers={"Accept-Encoding": "gzip"}).headers.get("Content-Encoding") is None


def test_series_falls_back_to_the_buffer_when_the_database_cannot_be_read(rig, monkeypatch):
    client, recorder, storage, _ = rig

    def broken(*args):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(storage, "series", broken)
    monkeypatch.setattr(storage, "raw_rows", broken)
    monkeypatch.setattr(storage, "sessions", broken)
    data = client.get("/api/series?window=21600&points=2000").get_json()
    assert sum(data["n"]) == 600  # the buffer's ten minutes, in the 6 h window
    assert "disk I/O error" in data["warning"]
    assert client.get("/api/config").get_json()["sessions"] == []


def test_orientation_with_the_field_along_one_axis(rig):
    # Two dead axes: the orientation card says why there is no heading,
    # rather than "Could not load the orientation: HTTP 500".
    client, recorder, *_ = rig
    for i in range(100):  # outnumbering the minute of real readings before them
        recorder.add(int((NOW - 0.9 + i * 0.009) * 1000), 0.0, 0.0, 48_000.0)
    response = client.get("/api/orientation")
    assert response.status_code == 200
    o = response.get_json()
    assert o["heading_true_deg"] is None and o["verdict"] == "check"
    assert any("no heading" in note for note in o["notes"])


# ── The page's script ────────────────────────────────────────────────────────
#
# app.js runs in node against a stand-in DOM, Plotly, fetch and timers, which
# drive it as a browser would: answers to its requests, timer ticks, a drag on
# the chart. Plotly's stand-in gives the element its event methods only when
# it first draws, as the real one does. Skipped where node is not installed.

APP_JS = Path(__file__).parent.parent / "retina_magnetometer" / "web" / "static" / "app.js"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")

HARNESS = r"""
const fs = require("fs");
const vm = require("vm");

class Element {
    constructor(tag) {
        this.tag = tag; this.textContent = ""; this.className = ""; this.title = ""; this.style = {};
        this.children = []; this.dataset = {}; this.listeners = {}; this.attributes = {}; this.clientWidth = 800;
        this.lastElementChild = { textContent: "" };
        const classes = new Set();
        this.classList = { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), contains: (c) => classes.has(c) };
    }
    set innerHTML(html) { this.html = html; this.children = []; }
    get innerHTML() { return this.html || ""; }
    append(...nodes) { this.children.push(...nodes); }
    setAttribute(k, v) { this.attributes[k] = v; }
    addEventListener(name, fn) { this.listeners[name] = fn; }
}
const elements = {};
const el = (id) => elements[id] || (elements[id] = new Element(id));
const buttons = [600, 3600, 21600, 86400, 604800, 2592000].map((w) => { const b = new Element("button"); b.dataset.window = String(w); return b; });
const factsOf = (id) => {
    const rows = [];
    const c = el(id).children;
    for (let i = 0; i + 1 < c.length; i += 2) rows.push([c[i].textContent, c[i + 1].textContent, c[i + 1].className]);
    return rows;
};

const plotly = { calls: [], react(node, data, layout) {
    this.calls.push({ data, layout });
    if (!node.on) { node.handlers = {}; node.on = (name, fn) => { node.handlers[name] = fn; }; }
    return Promise.resolve(node);
} };

const requests = [];
const pending = new Promise(() => {});
let route = () => pending;
async function fetch(url) {
    requests.push(url);
    const r = await route(url);
    return { ok: r.status >= 200 && r.status < 300, status: r.status, json: async () => r.body };
}
const timers = [];
const sandbox = {
    document: {
        getElementById: el,
        querySelectorAll: (selector) => (selector === ".seg-btn" ? buttons : []),
        createElement: (tag) => new Element(tag),
        createElementNS: (ns, tag) => new Element(tag),
    },
    window: { addEventListener() {} },
    location: { hash: "" },
    history: { replaceState() {} },
    localStorage: { getItem: () => null, setItem() {} },
    Plotly: plotly,
    fetch,
    URLSearchParams,
    setTimeout: (fn, ms) => timers.push({ fn, ms, done: false }),
    clearTimeout: (id) => { if (id && timers[id - 1]) timers[id - 1].done = true; },
};
const settle = async () => { for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r)); };
async function runTimers() {
    for (const timer of timers.filter((t) => !t.done)) { timer.done = true; timer.fn(); }
    await settle();
}
function load(hash) {
    sandbox.location.hash = hash || "";
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), sandbox);
}

// A server that buckets on a fixed grid, as the real one does: the value of a
// bucket depends on its index, and the newest one on how far it has filled.
const clock = { now: 1790769600000 };
function series(windowS, points, opts = {}) {
    const start = clock.now - windowS * 1000;
    const bucket = opts.bucket_ms !== undefined ? opts.bucket_ms : Math.ceil((windowS * 1000) / points);
    const out = { t: [], n: [], source: "memory", bucket_ms: bucket, start, end: clock.now, now: clock.now };
    for (const a of ["x", "y", "z", "b"]) out[a] = { min: [], mean: [], max: [] };
    const step = bucket || 1000;
    for (let k = Math.floor(start / step); k * step < clock.now; k++) {
        if (k * step < start && !bucket) continue;
        const filling = (k + 1) * step > clock.now ? (clock.now % step) / step : 1;
        const base = opts.value ? opts.value(k * step) : 16000 + (k % 13) + filling;
        out.t.push(k * step);
        out.n.push(bucket ? 2 : 1);
        for (const a of ["x", "y", "z", "b"]) {
            const spike = opts.spike ? opts.spike(k * step) : 0;
            out[a].min.push(base - 1 - spike);
            out[a].mean.push(base);
            out[a].max.push(base + 1 + spike);
        }
    }
    return out;
}
function answer(url, opts) {
    const q = new URLSearchParams(url.split("?")[1]);
    const data = series(Number(q.get("window")), Number(q.get("points")), opts);
    if (q.get("since") === null) return data;
    const first = data.t.findIndex((t) => t >= Number(q.get("since")));
    const from = first < 0 ? data.t.length : first;
    const tail = { ...data, since: Number(q.get("since")), t: data.t.slice(from), n: data.n.slice(from) };
    for (const a of ["x", "y", "z", "b"]) tail[a] = { min: data[a].min.slice(from), mean: data[a].mean.slice(from), max: data[a].max.slice(from) };
    return tail;
}
const meanTrace = (call, name) => call.data.find((trace) => trace.name === name);
const report = {};
const done = () => process.stdout.write(JSON.stringify(report));
"""


def run_page(script: str) -> dict:
    """Run app.js under the harness, driven by ``script``, which fills ``report``."""
    source = (
        HARNESS + "(async () => {\n" + script + "\n done();\n})().catch((e) => { console.error(e); process.exit(1); });"
    )
    result = subprocess.run([NODE, "-e", source, str(APP_JS)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@needs_node
def test_page_zoom_works_after_a_failed_first_load():
    # Plotly attaches the chart's event methods when it first draws. Wiring
    # the zoom straight after a first fetch that failed used to throw, and
    # left drag-to-zoom dead until the page was reloaded.
    report = run_page(
        """
        let seriesCalls = 0;
        route = (url) => {
            if (!url.startsWith("/api/series")) return pending;
            seriesCalls += 1;
            return seriesCalls === 1 ? { status: 500, body: {} } : { status: 200, body: answer(url) };
        };
        load("#window=3600");
        await settle();
        report.afterFailure = el("chart-note").textContent;
        await runTimers();
        report.wired = Boolean(el("chart").handlers && el("chart").handlers.plotly_relayout);
        el("chart").handlers.plotly_relayout({ "xaxis.range[0]": "2026-09-30T11:40:00Z", "xaxis.range[1]": "2026-09-30T11:45:00Z" });
        await settle();
        report.zoomed = requests[requests.length - 1];
        report.zoomNote = el("chart-note").textContent;
        el("chart").handlers.plotly_relayout({ "xaxis.autorange": true });
        await settle();
        report.reset = requests[requests.length - 1];
        """
    )
    assert report["afterFailure"] == "Could not load the chart: HTTP 500"
    assert report["wired"] is True
    assert "start=1790768400000&end=1790768700000" in report["zoomed"]
    assert "the zoomed range" in report["zoomNote"]
    assert report["reset"].startswith("/api/series?window=3600&points=1200")


@needs_node
def test_page_refreshes_a_window_with_only_the_new_points():
    # Each refresh asks for the points from its newest one on, and what it
    # draws is what a full fetch would have drawn.
    report = run_page(
        """
        route = (url) => (url.startsWith("/api/series") ? { status: 200, body: answer(url) } : pending);
        load("#window=3600");
        await settle();
        report.matches = [];
        report.urls = [requests[requests.length - 1]];
        for (let i = 0; i < 31; i++) {
            clock.now += 2000;
            await runTimers();
            report.urls.push(requests[requests.length - 1]);
            const drawn = meanTrace(plotly.calls[plotly.calls.length - 1], "X");
            const full = series(3600, 1200);
            report.matches.push(
                JSON.stringify(drawn.x.map((d) => d.getTime())) === JSON.stringify(full.t)
                    && JSON.stringify(drawn.y) === JSON.stringify(full.x.mean)
            );
        }
        // The server's buckets change (another width): the merge is refused,
        // and the same refresh fetches the window whole.
        const before = requests.length;
        route = (url) => (url.startsWith("/api/series") ? { status: 200, body: answer(url, { bucket_ms: 6000 }) } : pending);
        clock.now += 2000;
        await runTimers();
        report.afterChange = requests.slice(before);
        report.bucketAfterChange = meanTrace(plotly.calls[plotly.calls.length - 1], "X").x.length;
        """
    )
    assert all(report["matches"]), report["matches"]
    assert "since" not in report["urls"][0]
    assert all("&since=" in url for url in report["urls"][1:31])
    assert "since" not in report["urls"][31]  # a full fetch every 30 refreshes
    assert len(report["afterChange"]) == 2 and "since" not in report["afterChange"][1]
    assert report["bucketAfterChange"] == 601  # 6 s buckets on the grid, a part one at each end


@needs_node
@pytest.mark.parametrize(
    "bucket_ms,text", [(0, "every sample"), (338, "338 ms"), (2024, "2.0 s"), (18_000, "18 s"), (360_000, "6 min")]
)
def test_page_note_gives_the_bucket_width(bucket_ms, text):
    report = run_page(
        f"""
        route = (url) => (url.startsWith("/api/series") ? {{ status: 200, body: answer(url, {{ bucket_ms: {bucket_ms} }}) }} : pending);
        load("#window=600");
        await settle();
        report.note = el("chart-note").textContent;
        """
    )
    assert text in report["note"]
    assert "summarises 0 s" not in report["note"]


@needs_node
def test_page_long_windows_scale_to_the_daily_variation():
    # A week of minute summaries with passes in the band: the scale follows
    # the mean line's daily swing, the band runs off it, and the note says so.
    report = run_page(
        """
        const daily = (t) => 16000 + 30 * Math.sin((2 * Math.PI * t) / 86400000);
        const spike = (t) => (Math.floor(t / 360000) % 3 === 0 ? 900 : 0);
        route = (url) => (url.startsWith("/api/series") ? { status: 200, body: answer(url, { bucket_ms: 360000, value: daily, spike }) } : pending);
        load("#window=604800");
        await settle();
        const call = plotly.calls[plotly.calls.length - 1];
        report.week = call.layout.yaxis;
        report.weekB = call.layout.yaxis4;
        report.note = el("chart-note").textContent;
        report.hover = meanTrace(call, "X").hovertemplate;
        report.customdata = meanTrace(call, "X").customdata[0];
        await (async () => { for (const b of buttons) if (b.dataset.window === "600") b.listeners.click(); })();
        await settle();
        const short = plotly.calls[plotly.calls.length - 1];
        report.tenMinutes = short.layout.yaxis;
        report.tenMinutesNote = el("chart-note").textContent;
        report.tenMinutesBand = short.data.filter((trace) => trace.yaxis === "y").map((trace) => trace.y);
        """
    )
    low, high = report["week"]["range"]
    assert report["week"]["autorange"] is False and report["weekB"]["autorange"] is False
    assert 15_960 < low < 15_971 and 16_029 < high < 16_040  # the ±30 nT swing, padded
    assert "the band runs off them" in report["note"]
    assert "%{customdata[0]" in report["hover"] and report["customdata"][1] - report["customdata"][0] > 1000
    # Ten minutes is about the event, not the day: the band sets the scale.
    # This one hardly moves (no pass in it), so the scale is the narrowest,
    # 10 nT, around all of it.
    low, high = report["tenMinutes"]["range"]
    assert report["tenMinutes"]["autorange"] is False and high - low == pytest.approx(10)
    drawn = [v for trace in report["tenMinutesBand"] for v in trace if v is not None]
    assert low < min(drawn) and max(drawn) < high
    assert "runs off" not in report["tenMinutesNote"]


@needs_node
def test_page_widens_a_scale_too_narrow_for_whole_nanotesla_ticks():
    # A still sensor: the samples barely move, and a scale fitted to them
    # would have ticks a fraction of a nanotesla apart, labelled the same.
    report = run_page(
        """
        route = (url) => (url.startsWith("/api/series") ? { status: 200, body: answer(url, { bucket_ms: 6000, value: () => 22397.3 }) } : pending);
        load("#window=600");
        await settle();
        report.still = plotly.calls[plotly.calls.length - 1].layout.yaxis;
        report.stillNote = el("chart-note").textContent;
        const swing = (t) => 22397.3 + 20 * Math.sin(t / 60000);
        route = (url) => (url.startsWith("/api/series") ? { status: 200, body: answer(url, { bucket_ms: 6000, value: swing }) } : pending);
        await (async () => { for (const b of buttons) if (b.dataset.window === "3600") b.listeners.click(); })();
        await settle();
        report.moving = plotly.calls[plotly.calls.length - 1].layout.yaxis;
        """
    )
    assert report["still"]["autorange"] is False
    assert report["still"]["range"] == pytest.approx([22_392.3, 22_402.3])
    assert "runs off" not in report["stillNote"]
    assert report["moving"]["autorange"] is True and "range" not in report["moving"]


@needs_node
def test_page_shows_storage_problems_without_figures():
    # A database that never opened has no figures to show; its problem must
    # show all the same, with the database moved aside and the size cap.
    report = run_page(
        """
        const health = {
            state: "starting", state_text: "Starting up", detail: "", bus: null, last_sample: null, self_test: null,
            configured_rate_hz: 1, effective_rate_hz: null, measured_rate_hz: null, last_sample_at: null,
            samples_total: 0, read_errors_total: 0, consecutive_errors: 0, reinitialisations: 0, uptime_s: 3,
            recent_errors: [], storage: null, storage_error: "cannot open /data/magnetometer.sqlite: unable to open database file",
        };
        route = (url) => (url === "/api/health" ? { status: 200, body: health } : pending);
        load("#window=600");
        await settle();
        report.none = factsOf("storage-facts");
        health.storage_error = null;
        health.storage = {
            bytes: 4096, max_bytes: 1073741824, samples: 10, oldest_sample_ms: 1790769600000, minutes: 0,
            oldest_minute_ms: null, raw_retention_days: 7, rollup_retention_days: 365, size_capped_ms: 1790769000000,
            moved_aside: { at_ms: 1790769000000, reason: "file is not a database", kept_as: "magnetometer.sqlite.unreadable-20260930T115000Z" },
            set_aside: { count: 2, bytes: 3145728 },
        };
        await runTimers();
        report.figures = factsOf("storage-facts");
        """
    )
    assert report["none"] == [
        ["Problems", "cannot open /data/magnetometer.sqlite: unable to open database file", "bad"],
    ]
    labels = [row[0] for row in report["figures"]]
    assert labels == [
        "Database",
        "Raw samples",
        "Minute summaries",
        "Size cap",
        "Started afresh",
        "Kept aside",
        "Problems",
    ]
    assert "magnetometer.sqlite.unreadable-20260930T115000Z" in report["figures"][4][1]
    assert report["figures"][5][1].startswith("2 unreadable databases, 3.0 MB")
    assert report["figures"][6][1:] == ["none", ""]


@needs_node
def test_page_orientation_without_a_heading():
    report = run_page(
        """
        const o = {
            verdict: "check", measured: { total: 48000 }, samples: 60, magnitude_ratio: 0.99,
            reference: { total: 48564, inclination_deg: 62.3, declination_deg: -7.0 },
            down_axis: "+Z", up_axis: "-Z", tilt_min_deg: 27.7, heading_axis: null, heading_true_deg: null,
            heading_magnetic_deg: null, heading_sigma_deg: null, notes: ["The field has almost no horizontal part here."],
            location: null,
        };
        route = (url) => (url === "/api/orientation" ? { status: 200, body: o } : pending);
        load("#window=600");
        await settle();
        report.facts = factsOf("orient-facts");
        report.compass = el("compass").children.map((c) => c.textContent).filter(Boolean);
        """
    )
    assert ["Heading", "none (see below)", "warn"] in report["facts"]
    assert not any("null" in row[0] or "null" in row[1] for row in report["facts"])
    assert "no heading" in report["compass"]


@needs_node
@pytest.mark.parametrize(
    "true_deg,magnetic_deg,text",
    [
        (359.97, 6.99, "0.0° true (7.0° magnetic) ± 0.4°"),
        (359.94, 6.96, "359.9° true (7.0° magnetic) ± 0.4°"),
        (0.04, 359.96, "0.0° true (0.0° magnetic) ± 0.4°"),
        (180.05, 187.08, "180.1° true (187.1° magnetic) ± 0.4°"),
    ],
)
def test_page_heading_rounds_to_a_bearing(true_deg, magnetic_deg, text):
    # Rounded to a tenth, a heading a hair west of north would read 360.0°.
    report = run_page(
        f"""
        const o = {{
            verdict: "good", measured: {{ total: 48560 }}, samples: 60, magnitude_ratio: 1.0,
            reference: {{ total: 48564, inclination_deg: 62.3, declination_deg: -7.0 }},
            down_axis: "+Z", up_axis: "-Z", tilt_min_deg: 0.1, heading_axis: "+X", heading_true_deg: {true_deg},
            heading_magnetic_deg: {magnetic_deg}, heading_sigma_deg: 0.36, notes: [], location: null,
        }};
        route = (url) => (url === "/api/orientation" ? {{ status: 200, body: o }} : pending);
        load("#window=600");
        await settle();
        report.facts = factsOf("orient-facts");
        """
    )
    assert ["+X heading", text, ""] in report["facts"]


@needs_node
def test_page_gives_rates_the_app_worked_out_to_three_figures():
    # Poll mode clamps a rate it cannot keep to its ceiling, a figure worked
    # out to the last bit; the settings' own warning gives it as 87.3 Hz.
    report = run_page(
        """
        const config = {
            config: {
                bus: "/dev/i2c-1", i2c_framing: "repeated start", i2c_address: "auto (0x20-0x23)", mode: "poll",
                sample_rate_hz: 87.2580171575439, cycle_count: 200, self_test: true, raw_retention_days: 7,
                rollup_retention_days: 365, max_db_mb: 1024, flush_interval_s: 5, data_dir: "/data",
                node_config: "/config/config.yml", errors: [], warnings: [],
            },
            location: null,
        };
        const health = {
            state: "ok", state_text: "Sampling", detail: "", bus: "b", last_sample: null, self_test: null, mode: "poll",
            configured_rate_hz: 100, effective_rate_hz: 87.2580171575439, measured_rate_hz: 87.19364182510025,
            last_sample_at: null, samples_total: 10, read_errors_total: 0, consecutive_errors: 0,
            internal_errors_total: 0, reinitialisations: 0, uptime_s: 3, recent_errors: [], storage: null,
            storage_error: null, config_warnings: [], config_notes: [],
        };
        route = (url) => (url === "/api/config" ? { status: 200, body: config } : url === "/api/health" ? { status: 200, body: health } : pending);
        load("#window=600");
        await settle();
        report.config = factsOf("config-facts");
        report.health = factsOf("health-facts");
        """
    )
    assert ["Sampling", "poll, 87.3 Hz, 200 cycles", ""] in report["config"]
    assert ["Mode", "poll; 100 Hz configured · 87.3 Hz effective · 87.2 Hz measured", ""] in report["health"]


def test_series_with_an_empty_buffer_comes_from_disk(rig, tmp_path):
    # Just after a start, before the first sample: the disk alone.
    client, recorder, storage, health = rig
    fresh = Recorder(storage, health, recorder.config, clock=lambda: NOW)
    storage.write_samples([(int((NOW - 300 + i) * 1000), 1.0, 2.0, 3.0) for i in range(300)])
    app = create_app(Services(config=recorder.config, health=health, recorder=fresh, storage=storage, location=None))
    data = app.test_client().get(f"/api/series?start={int((NOW - 600) * 1000)}&end={int(NOW * 1000)}").get_json()
    assert data["source"] == "samples" and len(data["t"]) == 300


def test_series_too_long_to_join_comes_from_the_disk_alone(rig, monkeypatch):
    # Past MERGE_ROW_LIMIT raw rows the buffer's few unflushed seconds are not
    # worth the join; the disk's own aggregation answers.
    client, recorder, storage, _ = rig
    storage.write_samples([(int((NOW - 4200 + i) * 1000), 1.0, 2.0, 3.0) for i in range(3600)])
    monkeypatch.setattr(web, "MERGE_ROW_LIMIT", 1000)
    data = client.get("/api/series?window=5400&points=2000").get_json()
    assert data["source"] == "samples" and sum(data["n"]) == 3600


def test_orientation_without_samples_or_location(rig):
    client, recorder, storage, health = rig
    fresh = Recorder(storage, health, recorder.config, clock=lambda: NOW)
    app = create_app(
        Services(
            config=recorder.config, health=health, recorder=fresh, storage=storage, location=None, clock=lambda: NOW
        )
    )
    o = app.test_client().get("/api/orientation").get_json()
    assert o["verdict"] == "unknown" and o["samples"] == 0 and o["location"] is None and o["reference"] is None


@needs_node
@pytest.mark.parametrize(
    "window_s,bucket_ms,hours", [(604_800, 360_000, 1), (2_592_000, 1_500_000, 5), (86_400, 60_000, 0.2)]
)
def test_page_long_windows_keep_a_storm_in_view(window_s, bucket_ms, hours):
    # A storm or a bay is what the long views are for: shorter than 1 % of the
    # window, it once ran off a scale fitted to the line's percentiles.
    report = run_page(
        f"""
        const start = clock.now - {window_s} * 500 + 1800000, end = start + {hours} * 3600000;
        const value = (t) => 16000 + 30 * Math.sin((2 * Math.PI * t) / 86400000) + (t >= start && t < end ? -150 : 0);
        route = (url) => (url.startsWith("/api/series") ? {{ status: 200, body: answer(url, {{ bucket_ms: {bucket_ms}, value }}) }} : pending);
        load("#window={window_s}");
        await settle();
        const call = plotly.calls[plotly.calls.length - 1];
        const line = meanTrace(call, "X").y.filter((v) => v !== null);
        report.range = call.layout.yaxis.range;
        report.lineMin = Math.min(...line);
        report.lineMax = Math.max(...line);
        """
    )
    low, high = report["range"]
    assert report["lineMin"] < 15_860  # the storm is in the data drawn
    assert low < report["lineMin"] and report["lineMax"] < high


@needs_node
def test_page_fetches_the_whole_window_after_a_fallback():
    # A response from the live buffer alone carries a warning. Once the
    # database is back, the stored history must show at the next refresh,
    # not at the next full fetch, which for a week is hours away.
    report = run_page(
        """
        let failing = true;
        route = (url) => {
            if (!url.startsWith("/api/series")) return pending;
            const body = answer(url);
            if (failing) body.warning = "Stored history unavailable (disk I/O error); showing the live buffer only.";
            return { status: 200, body };
        };
        load("#window=3600");
        await settle();
        report.first = el("chart-note").textContent;
        failing = false;
        clock.now += 2000;
        await runTimers();
        report.afterRecovery = requests[requests.length - 1];
        clock.now += 2000;
        await runTimers();
        report.next = requests[requests.length - 1];
        report.note = el("chart-note").textContent;
        """
    )
    assert report["first"].startswith("Stored history unavailable")
    assert "since" not in report["afterRecovery"]
    assert "&since=" in report["next"] and "Stored history" not in report["note"]


@needs_node
def test_page_shows_configuration_warnings_internal_errors_and_the_capacity_note():
    # Warnings from the settings and from the running app (the listen
    # address, found unusable at bind) sit with the errors; the capacity note
    # is information, without alarm.
    report = run_page(
        """
        const note = "MAGNETOMETER_MAX_DB_MB=32 holds about 4.5 days of raw samples at 1 Hz, not the 7 days configured";
        const config = {
            config: {
                bus: "/dev/i2c-1", i2c_framing: "repeated start", i2c_address: "auto (0x20-0x23)", mode: "poll",
                sample_rate_hz: 1, cycle_count: 200, self_test: true, raw_retention_days: 7, rollup_retention_days: 365,
                max_db_mb: 32, flush_interval_s: 5, data_dir: "/data", node_config: "/config/config.yml",
                errors: ["MAGNETOMETER_MODE='burst' must be one of poll, continuous"],
                warnings: ["MAGNETOMETER_PORT='http' is not a port number (1..65535): the page is served on 3030"],
            },
            location: null,
        };
        const health = {
            state: "ok", state_text: "Sampling", detail: "", bus: "b", last_sample: null, self_test: null,
            configured_rate_hz: 1, effective_rate_hz: 1, measured_rate_hz: 1, last_sample_at: null,
            samples_total: 10, read_errors_total: 0, consecutive_errors: 0, internal_errors_total: 3,
            reinitialisations: 0, uptime_s: 3, recent_errors: [], storage: null, storage_error: null,
            config_warnings: [
                "MAGNETOMETER_HOST='192.0.2.1' cannot be listened on (Cannot assign requested address): the page is served on 127.0.0.1 only",
                "MAGNETOMETER_PORT='http' is not a port number (1..65535): the page is served on 3030",
            ],
            config_notes: [note],
        };
        route = (url) => (url === "/api/config" ? { status: 200, body: config } : url === "/api/health" ? { status: 200, body: health } : pending);
        load("#window=600");
        await settle();
        report.config = factsOf("config-facts");
        report.health = factsOf("health-facts");
        report.storage = factsOf("storage-facts");
        health.config_notes = [];
        health.internal_errors_total = 0;
        await runTimers();
        report.storageWithout = factsOf("storage-facts");
        report.healthWithout = factsOf("health-facts");
        delete health.config_notes;
        await runTimers();
        report.storageAbsent = factsOf("storage-facts");
        """
    )
    config = report["config"]
    assert ["Error", "MAGNETOMETER_MODE='burst' must be one of poll, continuous", "bad"] in config
    warnings = [row for row in config if row[0] == "Warning"]
    assert warnings == [
        ["Warning", "MAGNETOMETER_PORT='http' is not a port number (1..65535): the page is served on 3030", "warn"],
        [
            "Warning",
            "MAGNETOMETER_HOST='192.0.2.1' cannot be listened on (Cannot assign requested address): "
            "the page is served on 127.0.0.1 only",
            "warn",
        ],
    ]
    assert ["Internal errors", "3", "warn"] in report["health"]
    assert ["Internal errors", "0", ""] in report["healthWithout"]
    capacity = [row for row in report["storage"] if row[0] == "Capacity"]
    assert capacity == [
        [
            "Capacity",
            "MAGNETOMETER_MAX_DB_MB=32 holds about 4.5 days of raw samples at 1 Hz, not the 7 days configured",
            "",
        ]
    ]
    assert [row[0] for row in report["storageWithout"]] == ["Problems"]
    assert [row[0] for row in report["storageAbsent"]] == ["Problems"]
