/* The magnetometer page. Vanilla JS, no build step, like retina-gui.
 *
 * Polls the JSON API and redraws: the chart on a cadence that suits the
 * window (every 2 s for the live windows, minutes for the long ones), the
 * status every 2 s, the orientation every 10 s. Zooming the chart fetches the
 * zoomed range at full resolution; double-clicking returns to the window.
 *
 * A window refreshes with only what is new since the newest point the page
 * holds (the server's buckets sit on a fixed grid, so the rest is unchanged),
 * and in full every so often. Over a day or more the scales fit the mean
 * line, so the daily variation shows even when passing objects stretch the
 * min-max band far beyond it. */
(function () {
    "use strict";

    const AXES = [
        { key: "x", name: "X", colour: "#3b82f6", fill: "rgba(59,130,246,0.16)" },
        { key: "y", name: "Y", colour: "#10b981", fill: "rgba(16,185,129,0.16)" },
        { key: "z", name: "Z", colour: "#f59e0b", fill: "rgba(245,158,11,0.18)" },
        { key: "b", name: "|B|", colour: "#8b5cf6", fill: "rgba(139,92,246,0.16)" },
    ];
    const WINDOW_LABELS = { 600: "10 min", 3600: "1 h", 21600: "6 h", 86400: "24 h", 604800: "7 d", 2592000: "30 d" };
    const STATE_CLASS = { ok: "ok", starting: "warn", degraded: "warn", stalled: "warn", no_bus: "bad", no_sensor: "bad", config_error: "bad" };
    // Refreshes between full fetches of a window; the ones between bring only
    // the newest points.
    const FULL_EVERY = 30;
    // Spans from which the scales fit the mean line rather than the band.
    const FIT_FROM_MS = 86400000;

    const $ = (id) => document.getElementById(id);
    const chartEl = $("chart");

    let windowS = readWindow();
    let zoom = null; // {start, end} in ms while zoomed in
    let chartTimer = null;
    let chartBusy = false;
    let chartRequest = 0; // the newest refresh; an older one's answer is stale
    let held = null; // {url, data}: the series drawn last, which a refresh extends
    let sinceFull = 0;
    let zoomWired = false;
    let configRows = null; // the configuration card's settings, once loaded
    let configErrors = [];
    let settingWarnings = []; // found in the settings
    let runningWarnings = []; // found by the running app (health's config_warnings)

    // ── Formatting ──────────────────────────────────────────────────────────
    const nf0 = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 });
    const nf1 = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 1, minimumFractionDigits: 1 });
    const fmtNt = (v) => (v === null || v === undefined ? "–" : nf0.format(v));
    const fmtDeg = (v) => (v === null || v === undefined ? "–" : `${nf1.format(v)}°`);

    function fmtAge(seconds) {
        if (seconds === null || seconds === undefined) return "never";
        if (seconds < 2) return "just now";
        if (seconds < 90) return `${Math.round(seconds)} s ago`;
        if (seconds < 5400) return `${Math.round(seconds / 60)} min ago`;
        if (seconds < 172800) return `${Math.round(seconds / 3600)} h ago`;
        return `${Math.round(seconds / 86400)} days ago`;
    }

    // A bucket's width: under a second in milliseconds, not "0 s".
    function fmtDuration(ms) {
        if (ms < 1000) return `${Math.round(ms)} ms`;
        if (ms < 10000) return `${nf1.format(ms / 1000)} s`;
        if (ms < 120000) return `${Math.round(ms / 1000)} s`;
        if (ms < 7200000) return `${Math.round(ms / 60000)} min`;
        return `${nf1.format(ms / 3600000)} h`;
    }

    function fmtTime(iso) {
        if (!iso) return "–";
        const d = new Date(iso);
        return d.toLocaleString("en-GB", { hour12: false });
    }

    const fmtMs = (ms) => fmtTime(new Date(ms).toISOString());

    function fmtBytes(n) {
        if (n === null || n === undefined) return "–";
        const units = ["B", "KB", "MB", "GB"];
        let i = 0;
        while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
        return `${n.toFixed(i ? 1 : 0)} ${units[i]}`;
    }

    function facts(el, rows) {
        el.innerHTML = "";
        for (const [label, value, cls] of rows) {
            const dt = document.createElement("dt");
            dt.textContent = label;
            const dd = document.createElement("dd");
            dd.textContent = value;
            if (cls) dd.className = cls;
            el.append(dt, dd);
        }
    }

    // The server's own explanation of a failure, where it gave one.
    async function getJson(url) {
        const response = await fetch(url, { cache: "no-store" });
        if (!response.ok) {
            let reason = `HTTP ${response.status}`;
            try {
                const body = await response.json();
                if (body && body.error) reason = `${body.error} (HTTP ${response.status})`;
            } catch (e) { /* not JSON */ }
            throw new Error(reason);
        }
        return response.json();
    }

    // ── Window selection ────────────────────────────────────────────────────
    function readWindow() {
        const fromHash = Number(new URLSearchParams(location.hash.slice(1)).get("window"));
        if (WINDOW_LABELS[fromHash]) return fromHash;
        try {
            const stored = Number(localStorage.getItem("magnetometer.window"));
            if (WINDOW_LABELS[stored]) return stored;
        } catch (e) { /* storage may be unavailable */ }
        return 3600;
    }

    function setWindow(seconds) {
        windowS = seconds;
        zoom = null;
        try { localStorage.setItem("magnetometer.window", String(seconds)); } catch (e) { /* ignore */ }
        history.replaceState(null, "", `#window=${seconds}`);
        for (const b of document.querySelectorAll(".seg-btn")) b.classList.toggle("active", Number(b.dataset.window) === seconds);
        refreshChart(true);
    }

    function chartCadenceMs() {
        if (zoom) return null;
        if (windowS <= 3600) return 2000;
        if (windowS <= 86400) return 30000;
        return 300000;
    }

    // ── Chart data ──────────────────────────────────────────────────────────

    // What a full fetch would have returned, from the series held and the
    // points the server sent from `update.since` on: the held points before
    // that, those, and none from before the window's first bucket. Null when
    // the two do not fit together (other buckets, or a server clock that went
    // back), and the caller fetches the window whole.
    function mergeSeries(old, update) {
        if (update.since === undefined || update.bucket_ms !== old.bucket_ms || update.end < old.end) return null;
        const from = update.bucket_ms ? Math.floor(update.start / update.bucket_ms) * update.bucket_ms : update.start;
        let first = 0;
        while (first < old.t.length && old.t[first] < from) first++;
        let last = first;
        while (last < old.t.length && old.t[last] < update.since) last++;
        const join = (a, b) => a.slice(first, last).concat(b);
        const merged = { ...update, t: join(old.t, update.t), n: join(old.n, update.n) };
        for (const axis of AXES) {
            merged[axis.key] = {};
            for (const k of ["min", "mean", "max"]) merged[axis.key][k] = join(old[axis.key][k], update[axis.key][k]);
        }
        return merged;
    }

    // The whole range of the mean line, padded: a scale for the slow
    // variation, which the spikes in the band would flatten. A storm or a bay
    // moves the line, so it stays in view; a pass of a few seconds hardly
    // moves a bucket's mean, and only its band runs off the scale. Null when
    // there is no line.
    function fitRange(values) {
        let lo = Infinity;
        let hi = -Infinity;
        for (const v of values) {
            if (v === null) continue;
            if (v < lo) lo = v;
            if (v > hi) hi = v;
        }
        if (lo > hi) return null;
        const pad = Math.max(0.05 * (hi - lo), 5);
        return [lo - pad, hi + pad];
    }

    // Per axis, the range the y scale is held to (null: Plotly fits it), and
    // whether any of the band runs past one.
    function scales(data) {
        const span = zoom ? zoom.end - zoom.start : windowS * 1000;
        const ranges = AXES.map((axis) => (span >= FIT_FROM_MS && data.bucket_ms > 0 ? fitRange(data[axis.key].mean) : null));
        const clipped = AXES.some((axis, i) => {
            const r = ranges[i];
            const s = data[axis.key];
            return r !== null && (s.min.some((v) => v !== null && v < r[0]) || s.max.some((v) => v !== null && v > r[1]));
        });
        return { ranges, clipped };
    }

    // ── Chart ───────────────────────────────────────────────────────────────
    function traces(data) {
        const t = data.t.map((ms) => new Date(ms));
        const bucketed = data.bucket_ms > 0;
        const out = [];
        AXES.forEach((axis, i) => {
            const yaxis = i === 0 ? "y" : `y${i + 1}`;
            const s = data[axis.key];
            if (bucketed) {
                // Min then max filled to it: the envelope a long bucket hides.
                out.push({ x: t, y: s.min, xaxis: "x", yaxis, type: "scatter", mode: "lines", line: { width: 0 }, hoverinfo: "skip", showlegend: false, connectgaps: false });
                out.push({ x: t, y: s.max, xaxis: "x", yaxis, type: "scatter", mode: "lines", line: { width: 0 }, fill: "tonexty", fillcolor: axis.fill, hoverinfo: "skip", showlegend: false, connectgaps: false });
            }
            out.push({
                x: t, y: s.mean, xaxis: "x", yaxis, type: "scatter", mode: "lines", name: axis.name,
                line: { color: axis.colour, width: 1.4 }, connectgaps: false,
                // A bucket's full range, which the band may run off the scale to show.
                customdata: bucketed ? s.min.map((lo, k) => [lo, s.max[k]]) : undefined,
                hovertemplate: bucketed
                    ? `${axis.name} %{y:,.1f} nT (%{customdata[0]:,.0f} to %{customdata[1]:,.0f})<extra></extra>`
                    : `${axis.name} %{y:,.1f} nT<extra></extra>`,
            });
        });
        return out;
    }

    function layout(data, ranges) {
        const axisStyle = { gridcolor: "#eceae6", zeroline: false, tickformat: ",.0f", ticks: "outside", ticklen: 3, tickcolor: "#d8d6d2", automargin: true, title: { font: { size: 12, color: "#6b7280" } } };
        const range = zoom ? [new Date(zoom.start), new Date(zoom.end)] : [new Date(data.start), new Date(data.end)];
        const yaxis = (i, text) => ({ ...axisStyle, title: { ...axisStyle.title, text }, ...(ranges[i] ? { range: ranges[i], autorange: false } : { autorange: true }) });
        return {
            uirevision: zoom ? `zoom-${zoom.start}-${zoom.end}` : `window-${windowS}`,
            margin: { l: 70, r: 14, t: 6, b: 30 },
            paper_bgcolor: "rgba(0,0,0,0)",
            plot_bgcolor: "rgba(0,0,0,0)",
            font: { family: "Inter, system-ui, sans-serif", size: 12, color: "#374151" },
            hovermode: "x unified",
            showlegend: false,
            grid: { rows: 4, columns: 1, pattern: "coupled", roworder: "top to bottom", ygap: 0.12 },
            xaxis: { type: "date", range, gridcolor: "#eceae6", showspikes: true, spikemode: "across", spikethickness: 1, spikecolor: "#9ca3af" },
            yaxis: yaxis(0, "X (nT)"),
            yaxis2: yaxis(1, "Y (nT)"),
            yaxis3: yaxis(2, "Z (nT)"),
            yaxis4: yaxis(3, "|B| (nT)"),
        };
    }

    function describe(data, clipped) {
        const warning = data.warning ? `${data.warning} ` : "";
        const n = data.n.reduce((a, b) => a + b, 0);
        const span = zoom ? "the zoomed range" : `the last ${WINDOW_LABELS[windowS]}`;
        if (!data.t.length) return `${warning}No samples in ${span} yet.`;
        const source = { memory: "live buffer", samples: "stored samples", "samples+memory": "stored samples and the live buffer", minutes: "minute summaries" }[data.source] || data.source;
        const resolution = data.bucket_ms > 0 ? `each point summarises ${fmtDuration(data.bucket_ms)} (line: mean, band: min to max)` : "every sample";
        const scale = clipped ? " The scales fit the line, so the band runs off them where a spike passed; hover a point for its full range." : "";
        return `${warning}${nf0.format(n)} samples over ${span}, from the ${source}; ${resolution}.${scale} Drag to zoom, double-click to reset.`;
    }

    async function refreshChart(force) {
        if (chartBusy && !force) return;
        const request = ++chartRequest;
        chartBusy = true;
        const points = Math.max(300, Math.min(4000, Math.round(chartEl.clientWidth * 1.5)));
        const url = zoom
            ? `/api/series?start=${Math.round(zoom.start)}&end=${Math.round(zoom.end)}&points=${points}`
            : `/api/series?window=${windowS}&points=${points}`;
        try {
            let data = null;
            // A response from the live buffer alone (the database could not be
            // read) is not the window's history: the next one is fetched whole.
            if (!zoom && held && held.url === url && held.data.t.length && !held.data.warning && sinceFull < FULL_EVERY) {
                const newest = held.data.t[held.data.t.length - 1];
                const update = await getJson(`${url}&since=${newest}`);
                if (request !== chartRequest) return; // a window change or a zoom overtook it
                data = mergeSeries(held.data, update);
                sinceFull += 1;
            }
            if (!data) {
                data = await getJson(url);
                if (request !== chartRequest) return;
                sinceFull = 0;
            }
            held = { url, data };
            const { ranges, clipped } = scales(data);
            await Plotly.react(chartEl, traces(data), layout(data, ranges), { displayModeBar: false, responsive: true, scrollZoom: false });
            // Plotly gives the element its event methods when it first draws
            // into it, which a failed first fetch puts off.
            if (!zoomWired) {
                wireZoom();
                zoomWired = true;
            }
            $("chart-note").textContent = describe(data, clipped);
        } catch (err) {
            if (request !== chartRequest) return;
            held = null;
            $("chart-note").textContent = `Could not load the chart: ${err.message}`;
        } finally {
            if (request === chartRequest) {
                chartBusy = false;
                scheduleChart();
            }
        }
    }

    function scheduleChart() {
        clearTimeout(chartTimer);
        const cadence = chartCadenceMs();
        if (cadence !== null) chartTimer = setTimeout(() => refreshChart(false), cadence);
    }

    function wireZoom() {
        chartEl.on("plotly_relayout", (event) => {
            if (event["xaxis.autorange"]) {
                zoom = null;
                refreshChart(true);
                return;
            }
            const a = event["xaxis.range[0]"];
            const b = event["xaxis.range[1]"];
            if (a !== undefined && b !== undefined) {
                zoom = { start: new Date(a).getTime(), end: new Date(b).getTime() };
                refreshChart(true);
            }
        });
    }

    // ── Health, tiles, storage ─────────────────────────────────────────────
    function renderHealth(h) {
        const pill = $("state-pill");
        pill.className = `pill ${STATE_CLASS[h.state] || ""}`;
        pill.title = h.detail || "";
        $("state-text").textContent = h.state_text;
        $("last-age").textContent = fmtAge(h.last_sample_age_s);
        $("bus-label").textContent = h.bus || "no bus";

        const s = h.last_sample;
        $("tile-b").textContent = s ? fmtNt(s.b) : "–";
        $("tile-x").textContent = s ? fmtNt(s.x) : "–";
        $("tile-y").textContent = s ? fmtNt(s.y) : "–";
        $("tile-z").textContent = s ? fmtNt(s.z) : "–";

        $("health-detail").textContent = h.detail;
        const st = h.self_test;
        const selfTest = !st ? "not run" : st.passed ? `passed (${fmtTime(st.at)})` : `FAILED on ${["x", "y", "z"].filter((a) => !st[`${a}_ok`]).join(", ").toUpperCase() || "sensor"} (BIST ${st.raw})`;
        const rate = [
            `${h.configured_rate_hz} Hz configured`,
            h.effective_rate_hz !== null ? `${Number(h.effective_rate_hz.toPrecision(3))} Hz effective` : null,
            h.measured_rate_hz !== null ? `${Number(h.measured_rate_hz.toPrecision(3))} Hz measured` : null,
        ].filter(Boolean).join(" · ");
        facts($("health-facts"), [
            ["State", `${h.state_text}`, h.state === "ok" ? "" : STATE_CLASS[h.state] === "bad" ? "bad" : "warn"],
            ["Sensor", h.sensor ? `RM3100 at ${h.sensor.address}, REVID ${h.sensor.revid}` : "not found"],
            ["Cycle count", h.sensor ? `${h.sensor.cycle_count} (${h.sensor.gain_lsb_per_ut} counts/µT)` : "–"],
            ["Self test", selfTest, st && !st.passed ? "bad" : ""],
            ["Mode", `${h.mode}; ${rate}`],
            ["Last sample", h.last_sample_at ? `${fmtTime(h.last_sample_at)} (${fmtAge(h.last_sample_age_s)})` : "none yet"],
            ["Samples", nf0.format(h.samples_total)],
            ["Read errors", `${nf0.format(h.read_errors_total)} total, ${h.consecutive_errors} in a row`, h.consecutive_errors ? "warn" : ""],
            ["Internal errors", nf0.format(h.internal_errors_total || 0), h.internal_errors_total ? "warn" : ""],
            ["Re-initialised", `${h.reinitialisations} time${h.reinitialisations === 1 ? "" : "s"}`],
            ["Up for", fmtAge(h.uptime_s).replace(" ago", "")],
        ]);

        const list = $("health-errors");
        list.innerHTML = "";
        const errors = (h.recent_errors || []).slice().reverse();
        if (!errors.length) {
            const li = document.createElement("li");
            li.className = "muted";
            li.textContent = "None since the app started.";
            list.append(li);
        }
        for (const e of errors) {
            const li = document.createElement("li");
            const when = document.createElement("span");
            when.className = "when";
            when.textContent = fmtTime(e.at);
            li.append(when, `${e.kind}: ${e.message}`);
            list.append(li);
        }

        // The figures come from the database once a minute; its problems,
        // including not opening at all, are shown even when there are none.
        const db = h.storage;
        const used = db && db.max_bytes ? db.bytes / db.max_bytes : 0;
        const meter = $("storage-meter");
        meter.style.width = `${Math.min(100, used * 100).toFixed(1)}%`;
        meter.classList.toggle("warn", used > 0.85);
        const rows = [];
        if (db) {
            rows.push(
                ["Database", `${fmtBytes(db.bytes)} of ${fmtBytes(db.max_bytes)} cap`],
                ["Raw samples", `${nf0.format(db.samples)} kept ${db.raw_retention_days} days` + (db.oldest_sample_ms ? `, oldest ${fmtMs(db.oldest_sample_ms)}` : "")],
                ["Minute summaries", `${nf0.format(db.minutes)} kept ${db.rollup_retention_days} days` + (db.oldest_minute_ms ? `, oldest ${fmtMs(db.oldest_minute_ms)}` : "")],
            );
            if (db.size_capped_ms) rows.push(["Size cap", `reached ${fmtMs(db.size_capped_ms)}: the oldest data went to stay under it`, "warn"]);
            if (db.moved_aside) {
                const m = db.moved_aside;
                rows.push(["Started afresh", `${fmtMs(m.at_ms)}: the database could not be read (${m.reason}), so it was kept as ${m.kept_as} and a new one begun`, "warn"]);
            }
            // Never deleted by the app: the space is the operator's to reclaim.
            if (db.set_aside) {
                const k = db.set_aside;
                rows.push(["Kept aside", `${nf0.format(k.count)} unreadable database${k.count === 1 ? "" : "s"}, ${fmtBytes(k.bytes)}, beside this one: delete once no longer needed`, "warn"]);
            }
        }
        // What the settings mean for the history kept (the size cap holding
        // less than the retentions ask for): information, not a fault.
        for (const note of h.config_notes || []) rows.push(["Capacity", note]);
        rows.push(["Problems", h.storage_error || "none", h.storage_error ? "bad" : ""]);
        facts($("storage-facts"), rows);

        const warnings = h.config_warnings || [];
        if (warnings.join("\n") !== runningWarnings.join("\n")) {
            runningWarnings = warnings;
            renderConfig();
        }
    }

    async function refreshHealth() {
        try {
            renderHealth(await getJson("/api/health"));
        } catch (err) {
            $("state-text").textContent = "App not responding";
            $("state-pill").className = "pill bad";
        } finally {
            setTimeout(refreshHealth, 2000);
        }
    }

    // ── Orientation ─────────────────────────────────────────────────────────
    const UNIT = { "+X": [1, 0, 0], "-X": [-1, 0, 0], "+Y": [0, 1, 0], "-Y": [0, -1, 0], "+Z": [0, 0, 1], "-Z": [0, 0, -1] };
    function axisName(v) {
        for (const [name, u] of Object.entries(UNIT)) if (u[0] === v[0] && u[1] === v[1] && u[2] === v[2]) return name;
        return "?";
    }
    const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
    const hasHeading = (o) => o && o.heading_true_deg !== null && o.heading_true_deg !== undefined;

    function drawCompass(o) {
        const svg = $("compass");
        const ns = "http://www.w3.org/2000/svg";
        svg.innerHTML = "";
        const el = (tag, attrs, text) => {
            const node = document.createElementNS(ns, tag);
            for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
            if (text !== undefined) node.textContent = text;
            svg.append(node);
            return node;
        };
        el("circle", { cx: 0, cy: 0, r: 96, fill: "#fbfaf8", stroke: "#e2e0dc" });
        for (let d = 0; d < 360; d += 10) {
            const r1 = d % 90 === 0 ? 84 : d % 30 === 0 ? 88 : 91;
            const a = (d - 90) * Math.PI / 180;
            el("line", { x1: 96 * Math.cos(a), y1: 96 * Math.sin(a), x2: r1 * Math.cos(a), y2: r1 * Math.sin(a), stroke: "#c9c6c0", "stroke-width": d % 90 === 0 ? 1.5 : 1 });
        }
        for (const [label, d] of [["N", 0], ["E", 90], ["S", 180], ["W", 270]]) {
            const a = (d - 90) * Math.PI / 180;
            el("text", { x: 72 * Math.cos(a), y: 72 * Math.sin(a) + 4, "text-anchor": "middle", "font-size": 12, "font-weight": 600, fill: d === 0 ? "#111827" : "#6b7280" }, label);
        }
        if (!hasHeading(o)) {
            // With samples and still no heading, the notes below say why.
            el("text", { x: 0, y: 4, "text-anchor": "middle", "font-size": 11, fill: "#9ca3af" }, o && o.samples ? "no heading" : "no heading yet");
            $("compass-caption").textContent = "";
            return;
        }
        // Magnetic north, dashed, at the declination.
        const dec = o.reference.declination_deg;
        const am = (dec - 90) * Math.PI / 180;
        el("line", { x1: 0, y1: 0, x2: 58 * Math.cos(am), y2: 58 * Math.sin(am), stroke: "#9ca3af", "stroke-dasharray": "3 3", "stroke-width": 1.2 });
        const side = dec < 0 ? "end" : "start";
        el("text", { x: 50 * Math.cos(am) + (dec < 0 ? -6 : 6), y: 50 * Math.sin(am), "text-anchor": side, "font-size": 9, fill: "#9ca3af" }, "mag N");

        // The sensor, seen from above, rotated to its heading.
        const heading = o.heading_true_deg;
        const g = document.createElementNS(ns, "g");
        g.setAttribute("transform", `rotate(${heading})`);
        svg.append(g);
        const add = (tag, attrs, text) => {
            const node = document.createElementNS(ns, tag);
            for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
            if (text !== undefined) node.textContent = text;
            g.append(node);
            return node;
        };
        add("rect", { x: -22, y: -22, width: 44, height: 44, rx: 6, fill: "#ffffff", stroke: "#374151", "stroke-width": 1.5 });
        add("line", { x1: 0, y1: 0, x2: 0, y2: -58, stroke: "#3b82f6", "stroke-width": 3, "stroke-linecap": "round" });
        add("path", { d: "M0,-66 L-6,-54 L6,-54 Z", fill: "#3b82f6" });
        add("text", { x: 0, y: -70, "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: "#3b82f6", transform: `rotate(${-heading} 0 -74)` }, o.heading_axis);
        // The other horizontal axis is 90° clockwise of the heading axis when
        // seen from above: down x heading, in the sensor's own coordinates.
        const second = axisName(cross(UNIT[o.down_axis], UNIT[o.heading_axis]));
        add("line", { x1: 0, y1: 0, x2: 44, y2: 0, stroke: "#10b981", "stroke-width": 2.5, "stroke-linecap": "round" });
        add("path", { d: "M52,0 L41,-5 L41,5 Z", fill: "#10b981" });
        add("text", { x: 60, y: 4, "text-anchor": "start", "font-size": 10, "font-weight": 700, fill: "#10b981", transform: `rotate(${-heading} 60 0)` }, second);
        // The vertical axis, seen end-on from above: a dot when it points up
        // at the viewer, a cross when it points down, the drafting convention.
        add("circle", { cx: 0, cy: 0, r: 7, fill: "#ffffff", stroke: "#374151", "stroke-width": 1.4 });
        const up = o.up_axis;
        add("circle", { cx: 0, cy: 0, r: 2.2, fill: "#374151" });
        $("compass-caption").textContent = `Seen from above; the dot is ${up}, pointing up.`;
    }

    function renderOrientation(o) {
        const verdict = $("orient-verdict");
        const text = { good: "Consistent with WMM2025", check: "Worth a look", unknown: "Not enough to go on" }[o.verdict];
        verdict.className = `pill small ${{ good: "ok", check: "warn", unknown: "" }[o.verdict]}`;
        verdict.lastElementChild.textContent = text;
        drawCompass(o);
        const ref = o.reference;
        const rows = [["Measured |B|", `${fmtNt(o.measured.total)} nT (median of ${nf0.format(o.samples)} samples)`]];
        if (ref) {
            const pct = (o.magnitude_ratio - 1) * 100;
            rows.push(["Model |B|", `${fmtNt(ref.total)} nT, dip ${fmtDeg(ref.inclination_deg)}, declination ${fmtDeg(ref.declination_deg)}`]);
            rows.push(["Difference", `${pct >= 0 ? "+" : ""}${nf1.format(pct)} %`, Math.abs(pct) > 5 ? "warn" : ""]);
        }
        if (o.down_axis) {
            rows.push(["Pointing down", `${o.down_axis} (tilted at least ${fmtDeg(o.tilt_min_deg)})`, o.tilt_min_deg > 10 ? "warn" : ""]);
            if (hasHeading(o)) rows.push([`${o.heading_axis} heading`, `${fmtDeg(o.heading_true_deg)} true (${fmtDeg(o.heading_magnetic_deg)} magnetic) ± ${nf1.format(o.heading_sigma_deg)}°`]);
            else rows.push(["Heading", "none (see below)", "warn"]);
        }
        facts($("orient-facts"), rows);
        const notes = $("orient-notes");
        notes.innerHTML = "";
        for (const n of o.notes) {
            const li = document.createElement("li");
            li.textContent = n;
            notes.append(li);
        }
        if (o.location) $("foot-location").innerHTML = `Location <span class="mono">${o.location.latitude.toFixed(3)}, ${o.location.longitude.toFixed(3)}</span> (${o.location.source.split(" (")[0]})`;
    }

    async function refreshOrientation() {
        try {
            renderOrientation(await getJson("/api/orientation"));
        } catch (err) {
            $("orient-notes").innerHTML = "";
            const li = document.createElement("li");
            li.textContent = `Could not load the orientation: ${err.message}`;
            $("orient-notes").append(li);
        } finally {
            setTimeout(refreshOrientation, 10000);
        }
    }

    // ── Configuration ───────────────────────────────────────────────────────
    async function loadConfig() {
        try {
            const c = await getJson("/api/config");
            const cfg = c.config;
            configRows = [
                ["Bus", `${cfg.bus} (${cfg.i2c_framing})`],
                ["Address", cfg.i2c_address],
                ["Sampling", `${cfg.mode}, ${cfg.sample_rate_hz} Hz, ${cfg.cycle_count} cycles`],
                ["Self test at start", cfg.self_test ? "yes" : "no"],
                ["Retention", `raw ${cfg.raw_retention_days} days, minutes ${cfg.rollup_retention_days} days, cap ${cfg.max_db_mb} MB`],
                ["Writes", `every ${cfg.flush_interval_s} s to ${cfg.data_dir}`],
                ["Location", c.location ? `${c.location.latitude.toFixed(4)}, ${c.location.longitude.toFixed(4)}, ${nf0.format(c.location.altitude_m)} m (${c.location.source})` : `unknown: set location.rx in ${cfg.node_config} or MAGNETOMETER_LATITUDE/LONGITUDE`],
            ];
            configErrors = cfg.errors;
            settingWarnings = cfg.warnings || [];
        } catch (err) {
            configRows = [["Error", err.message, "bad"]];
            configErrors = [];
        }
        renderConfig();
    }

    // The settings, then what is wrong with them: errors (sampling is off),
    // and warnings, a setting replaced by a safe one, whether the settings
    // found it or the running app did (the listen address, at bind).
    function renderConfig() {
        if (configRows === null) return;
        const rows = configRows.slice();
        for (const e of configErrors) rows.push(["Error", e, "bad"]);
        for (const w of new Set([...settingWarnings, ...runningWarnings])) rows.push(["Warning", w, "warn"]);
        facts($("config-facts"), rows);
    }

    // A link or the back button can change the window through the hash.
    window.addEventListener("hashchange", () => {
        const w = Number(new URLSearchParams(location.hash.slice(1)).get("window"));
        if (WINDOW_LABELS[w] && w !== windowS) setWindow(w);
    });

    // ── Start ───────────────────────────────────────────────────────────────
    for (const b of document.querySelectorAll(".seg-btn")) b.addEventListener("click", () => setWindow(Number(b.dataset.window)));
    for (const b of document.querySelectorAll(".seg-btn")) b.classList.toggle("active", Number(b.dataset.window) === windowS);
    refreshChart(true);
    refreshHealth();
    refreshOrientation();
    loadConfig();
})();
