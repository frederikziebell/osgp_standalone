"""Built-in LAN dashboard: a stdlib-only HTTP server exposing the meter reader's live
readings as JSON, plus a static HTML page that polls them.

No third-party dependencies (Flask, etc.) - just http.server, so it costs nothing extra
to run on a Pi. Not authenticated: anyone on the LAN that can reach the bound
address/port can view live readings. Keep it off a network you don't trust, or bind it
to localhost and reach it over an SSH tunnel / VPN instead.
"""

import json
import logging
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from history import RANGE_PRESETS, query_range_preset
from sysinfo import get_system_stats

logger = logging.getLogger("Dashboard")

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Smart Meter Live Dashboard</title>
<style>
  :root {
    color-scheme: light;
    --page:      #f9f9f7;
    --surface:   #fcfcfb;
    --text:      #0b0b0b;
    --text-2:    #52514e;
    --muted:     #898781;
    --border:    rgba(11,11,11,0.10);
    --accent:    #2a78d6;
    --good:      #0ca30c;
    --warning:   #fab219;
    --critical:  #d03b3b;
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      color-scheme: dark;
      --page:      #0d0d0d;
      --surface:   #1a1a19;
      --text:      #ffffff;
      --text-2:    #c3c2b7;
      --muted:     #898781;
      --border:    rgba(255,255,255,0.10);
      --accent:    #3987e5;
      --good:      #0ca30c;
      --critical:  #e66767;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: var(--page);
    color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  .wrap { max-width: 900px; margin: 0 auto; padding: 24px 16px 48px; }
  header { display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px 16px; margin-bottom: 20px; }
  h1 { font-size: 20px; font-weight: 600; margin: 0; }
  .status-col { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }
  @media (max-width: 480px) {
    /* Once the title no longer fits alongside it, .status-col wraps onto its own line -
       a lone item on a wrapped flex line doesn't reliably stay pinned to the end edge
       under space-between, which is what left it stranded near the center. Stack and
       stretch explicitly instead of relying on that fallback. */
    header { flex-direction: column; align-items: stretch; }
    .status-col { width: 100%; }
  }
  .status { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-2); }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); flex: none; }
  .dot.good { background: var(--good); }
  .dot.critical { background: var(--critical); }
  .sysinfo { font-size: 11px; color: var(--text-2); }
  .sysinfo .stat-warning { color: var(--warning); font-weight: 600; }
  .sysinfo .stat-critical { color: var(--critical); font-weight: 700; }
  .hero {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 24px;
    margin-bottom: 16px;
  }
  .hero .label { font-size: 13px; color: var(--text-2); margin-bottom: 6px; }
  .hero .value { font-size: 48px; font-weight: 600; line-height: 1.1; }
  .hero .value .unit { font-size: 20px; font-weight: 500; color: var(--text-2); margin-left: 6px; }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 12px;
  }
  /* Sections with a fixed triplet (Power/Current/Voltage: 3 tiles each) use a fixed
     3-column grid instead of auto-fit - auto-fit wraps an odd tile count onto its own
     half-empty row on narrow screens (2 tiles, then 1 alone), which looks broken on a
     phone. Fixed columns just shrink each tile instead of ever wrapping. */
  .grid-3 { grid-template-columns: repeat(3, 1fr); }
  .tile {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
  }
  .tile .label { font-size: 13px; color: var(--text-2); margin-bottom: 4px; }
  .tile .value { font-size: 22px; font-weight: 600; }
  .tile .value .unit { font-size: 13px; font-weight: 500; color: var(--text-2); margin-left: 4px; }
  @media (max-width: 480px) {
    .grid-3 { gap: 8px; }
    .grid-3 .tile { padding: 10px; }
    .grid-3 .tile .value { font-size: 17px; }
    .grid-3 .tile .value .unit { font-size: 11px; margin-left: 2px; }
  }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; margin: 24px 0 10px; }
  footer { margin-top: 24px; font-size: 12px; color: var(--muted); }

  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 16px;
    margin-top: 24px;
  }
  .chart-controls { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
  .chart-controls select {
    font: inherit; font-size: 13px; color: var(--text); background: var(--page);
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px;
  }
  .range-buttons { display: flex; gap: 4px; }
  .range-buttons button {
    font: inherit; font-size: 13px; color: var(--text-2); background: transparent;
    border: 1px solid var(--border); border-radius: 6px; padding: 6px 12px; cursor: pointer;
  }
  .range-buttons button.active { color: var(--text); background: var(--page); border-color: var(--accent); }
  .chart-svg-wrap { position: relative; }
  .chart-svg-wrap svg { display: block; width: 100%; height: 260px; overflow: visible; }
  .chart-gridline { stroke: var(--border); stroke-width: 1; }
  .chart-axis-label { fill: var(--muted); font-size: 11px; }
  .chart-line { fill: none; stroke: var(--accent); stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  .chart-area { fill: var(--accent); opacity: 0.10; }
  .chart-crosshair { stroke: var(--muted); stroke-width: 1; stroke-dasharray: 3 3; }
  .chart-dot { fill: var(--accent); stroke: var(--surface); stroke-width: 2; }
  .chart-empty { fill: var(--muted); font-size: 13px; }
  .chart-tooltip {
    position: absolute; pointer-events: none; background: var(--surface); border: 1px solid var(--border);
    border-radius: 6px; padding: 6px 10px; font-size: 12px; color: var(--text); white-space: nowrap;
    box-shadow: 0 2px 8px rgba(0,0,0,0.15); transform: translate(-50%, -110%);
  }
  .chart-tooltip .t-time { color: var(--text-2); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Smart Meter Live Dashboard</h1>
    <div class="status-col">
      <div class="status"><span class="dot" id="dot"></span><span id="status-text">Loading...</span></div>
      <div class="sysinfo" id="sysinfo"></div>
    </div>
  </header>

  <div class="hero">
    <div class="label">Forward active power</div>
    <div class="value" id="hero-power">&#8211;<span class="unit">W</span></div>
  </div>

  <div class="section-title">Power</div>
  <div class="grid grid-3">
    <div class="tile"><div class="label">Reverse active power</div><div class="value" id="rev-power">&#8211;<span class="unit">W</span></div></div>
    <div class="tile"><div class="label">Import reactive power</div><div class="value" id="import-var">&#8211;<span class="unit">VAr</span></div></div>
    <div class="tile"><div class="label">Export reactive power</div><div class="value" id="export-var">&#8211;<span class="unit">VAr</span></div></div>
  </div>

  <div class="section-title">Current</div>
  <div class="grid grid-3">
    <div class="tile"><div class="label">L1</div><div class="value" id="l1-a">&#8211;<span class="unit">A</span></div></div>
    <div class="tile"><div class="label">L2</div><div class="value" id="l2-a">&#8211;<span class="unit">A</span></div></div>
    <div class="tile"><div class="label">L3</div><div class="value" id="l3-a">&#8211;<span class="unit">A</span></div></div>
  </div>

  <div class="section-title">Voltage</div>
  <div class="grid grid-3">
    <div class="tile"><div class="label">L1</div><div class="value" id="l1-v">&#8211;<span class="unit">V</span></div></div>
    <div class="tile"><div class="label">L2</div><div class="value" id="l2-v">&#8211;<span class="unit">V</span></div></div>
    <div class="tile"><div class="label">L3</div><div class="value" id="l3-v">&#8211;<span class="unit">V</span></div></div>
  </div>

  <div class="section-title">Energy (cumulative)</div>
  <div class="grid">
    <div class="tile"><div class="label">Forward active</div><div class="value" id="fwd-kwh">&#8211;<span class="unit">kWh</span></div></div>
    <div class="tile"><div class="label">Reverse active</div><div class="value" id="rev-kwh">&#8211;<span class="unit">kWh</span></div></div>
  </div>

  <footer id="footer">Waiting for first reading&hellip;</footer>

  <div class="chart-card">
    <div class="chart-controls">
      <select id="metric-select"></select>
      <div class="range-buttons" id="range-buttons">
        <button data-range="24h" class="active">24h</button>
        <button data-range="week">Week</button>
        <button data-range="month">Month</button>
        <button data-range="year">Year</button>
      </div>
    </div>
    <div class="chart-svg-wrap" id="chart-wrap">
      <svg id="chart-svg" viewBox="0 0 800 260" preserveAspectRatio="none"></svg>
      <div class="chart-tooltip" id="chart-tooltip" hidden></div>
    </div>
  </div>
</div>

<script>
const STALE_AFTER_MS = 30000;

function fmt(n, digits) {
  if (n === null || n === undefined) return "–";
  return Number(n).toLocaleString(undefined, {
    minimumFractionDigits: digits, maximumFractionDigits: digits
  });
}

function setValue(id, text) {
  const el = document.getElementById(id);
  const unit = el.querySelector(".unit");
  el.firstChild.textContent = text;
  if (unit) el.appendChild(unit);
}

async function poll() {
  let data;
  try {
    const res = await fetch("/api/live", { cache: "no-store" });
    data = await res.json();
  } catch (e) {
    setStatus(false, "Dashboard lost contact with the reader process");
    return;
  }

  setValue("hero-power", fmt(data.fwd_active_power_w, 0));
  setValue("rev-power", fmt(data.rev_active_power_w, 0));
  setValue("import-var", fmt(data.import_reactive_var, 0));
  setValue("export-var", fmt(data.export_reactive_var, 0));
  setValue("l1-a", fmt(data.l1_current_a, 3));
  setValue("l2-a", fmt(data.l2_current_a, 3));
  setValue("l3-a", fmt(data.l3_current_a, 3));
  setValue("l1-v", fmt(data.l1_voltage_v, 1));
  setValue("l2-v", fmt(data.l2_voltage_v, 1));
  setValue("l3-v", fmt(data.l3_voltage_v, 1));
  setValue("fwd-kwh", data.fwd_active_energy_wh != null ? fmt(data.fwd_active_energy_wh / 1000, 3) : null);
  setValue("rev-kwh", data.rev_active_energy_wh != null ? fmt(data.rev_active_energy_wh / 1000, 3) : null);

  const footer = document.getElementById("footer");
  if (data.last_update) {
    const age = Date.now() - new Date(data.last_update).getTime();
    const stale = age > STALE_AFTER_MS;
    footer.textContent = "Last meter reading: " + new Date(data.last_update).toLocaleString();
    if (!data.connected && stale) {
      setStatus(false, "No live session - last good reading " + Math.round(age / 1000) + "s ago");
    } else if (stale) {
      setStatus(false, "Readings are stale (" + Math.round(age / 1000) + "s old)");
    } else {
      setStatus(true, "Connected");
    }
  } else {
    setStatus(false, "No readings yet");
  }
}

function setStatus(good, text) {
  document.getElementById("dot").className = "dot " + (good ? "good" : "critical");
  document.getElementById("status-text").textContent = text;
}

poll();
setInterval(poll, 2000);

// ---------------------------------------------------------------------
// System health - small, muted by default; only calls attention to itself
// (color) when a value is actually out of the ordinary for a Pi.
// ---------------------------------------------------------------------

const TEMP_WARN_C = 70, TEMP_CRIT_C = 80;
const MEM_WARN_PCT = 85, MEM_CRIT_PCT = 95;

function severityClass(value, warnAt, critAt) {
  if (value === null || value === undefined) return "";
  if (value >= critAt) return "stat-critical";
  if (value >= warnAt) return "stat-warning";
  return "";
}

function formatBytes(bytes) {
  if (bytes < 1024) return bytes + " B";
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024, i = 0;
  while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
  return value.toFixed(1) + " " + units[i];
}

async function pollSystem() {
  let data;
  try {
    const res = await fetch("/api/system", { cache: "no-store" });
    data = await res.json();
  } catch (e) {
    return; // leave whatever was last shown - a failed system-stats fetch isn't worth alarming over
  }

  const parts = [];
  if (data.temp_c !== null && data.temp_c !== undefined) {
    const cls = severityClass(data.temp_c, TEMP_WARN_C, TEMP_CRIT_C);
    parts.push(`<span class="${cls}">${data.temp_c.toFixed(1)}&deg;C</span>`);
  }
  if (data.cpu_load_1m !== null && data.cpu_load_1m !== undefined) {
    // Unix load average is "cores wanted", not a percentage - a 1-min load of 1.0 on
    // a quad-core Pi is one core's worth of work, i.e. ~25% of total capacity. Scale
    // by core count so this reads as the "% of total CPU capacity" a percentage sign
    // implies (100% = all cores fully busy), same thresholds as before just rescaled.
    const cores = data.cpu_count || 4;
    const loadPercent = (data.cpu_load_1m / cores) * 100;
    const cls = severityClass(loadPercent, 100, 200);
    parts.push(`<span class="${cls}">CPU ${Math.round(loadPercent)}%</span>`);
  }
  if (data.mem_percent !== null && data.mem_percent !== undefined) {
    const cls = severityClass(data.mem_percent, MEM_WARN_PCT, MEM_CRIT_PCT);
    parts.push(`<span class="${cls}">mem ${Math.round(data.mem_percent)}%</span>`);
  }
  if (data.db_bytes !== null && data.db_bytes !== undefined) {
    parts.push(`db ${formatBytes(data.db_bytes)}`);
  }
  if (data.power) {
    const p = data.power;
    let cls = "", label = "power OK";
    if (p.undervoltage_now || p.throttled_now) {
      cls = "stat-critical";
      label = "power " + (p.undervoltage_now ? "undervoltage" : "throttled") + " now";
    } else if (p.undervoltage_ever || p.throttled_ever) {
      cls = "stat-warning";
      label = "power issue since boot";
    }
    parts.push(`<span class="${cls}">${label}</span>`);
  }
  document.getElementById("sysinfo").innerHTML = parts.join(" &middot; ");
}

pollSystem();
setInterval(pollSystem, 5000);

// ---------------------------------------------------------------------
// History chart - hand-drawn SVG line chart, no charting library needed.
// ---------------------------------------------------------------------

const METRICS = [
  { field: "fwd_active_power_w", label: "Forward active power", unit: "W", digits: 0 },
  { field: "rev_active_power_w", label: "Reverse active power", unit: "W", digits: 0 },
  { field: "import_reactive_var", label: "Import reactive power", unit: "VAr", digits: 0 },
  { field: "export_reactive_var", label: "Export reactive power", unit: "VAr", digits: 0 },
  { field: "l1_current_a", label: "L1 current", unit: "A", digits: 2 },
  { field: "l2_current_a", label: "L2 current", unit: "A", digits: 2 },
  { field: "l3_current_a", label: "L3 current", unit: "A", digits: 2 },
  { field: "l1_voltage_v", label: "L1 voltage", unit: "V", digits: 1 },
  { field: "l2_voltage_v", label: "L2 voltage", unit: "V", digits: 1 },
  { field: "l3_voltage_v", label: "L3 voltage", unit: "V", digits: 1 },
  { field: "fwd_active_energy_wh", label: "Forward active energy", unit: "Wh", digits: 0 },
  { field: "rev_active_energy_wh", label: "Reverse active energy", unit: "Wh", digits: 0 },
];

const metricSelect = document.getElementById("metric-select");
for (const m of METRICS) {
  const opt = document.createElement("option");
  opt.value = m.field;
  opt.textContent = m.label;
  metricSelect.appendChild(opt);
}

let currentRange = "24h";
let currentHistory = null;

function rangeLabel(range, ts) {
  const d = new Date(ts * 1000);
  if (range === "24h") return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (range === "week") return d.toLocaleDateString([], { weekday: "short" }) + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (range === "year") return d.toLocaleDateString([], { month: "short", year: "2-digit" });
  return d.toLocaleDateString([], { month: "short", day: "numeric" });
}

async function loadHistory() {
  try {
    const res = await fetch("/api/history?range=" + encodeURIComponent(currentRange), { cache: "no-store" });
    if (!res.ok) { currentHistory = null; drawChart(); return; }
    currentHistory = await res.json();
  } catch (e) {
    currentHistory = null;
  }
  drawChart();
}

// "Nice numbers for graph labels" (Heckbert) - picks round tick steps (1/2/5 x 10^n)
// instead of dividing the range into arbitrary equal parts, so the gap between any
// two axis labels is always something like 500 or 0.2, never 405.8.
function niceNumber(range, round) {
  const exponent = Math.floor(Math.log10(range));
  const fraction = range / Math.pow(10, exponent);
  let niceFraction;
  if (round) {
    if (fraction < 1.5) niceFraction = 1;
    else if (fraction < 3) niceFraction = 2;
    else if (fraction < 7) niceFraction = 5;
    else niceFraction = 10;
  } else {
    if (fraction <= 1) niceFraction = 1;
    else if (fraction <= 2) niceFraction = 2;
    else if (fraction <= 5) niceFraction = 5;
    else niceFraction = 10;
  }
  return niceFraction * Math.pow(10, exponent);
}

function niceAxisTicks(dataMin, dataMax, targetTicks) {
  if (dataMin === dataMax) dataMax = dataMin + 1;
  const range = niceNumber(dataMax - dataMin, false);
  const step = niceNumber(range / (targetTicks - 1), true);
  const min = Math.floor(dataMin / step) * step;
  const max = Math.ceil(dataMax / step) * step;
  const decimals = Math.max(0, -Math.floor(Math.log10(step) + 1e-9));
  return { min, max, step, decimals };
}

function drawChart() {
  const svg = document.getElementById("chart-svg");
  // Match the viewBox to the actual rendered width instead of a fixed 800 - with
  // preserveAspectRatio="none", a fixed viewBox gets non-uniformly stretched/squeezed
  // to fit whatever box the CSS actually gives it, which distorts strokes and (most
  // visibly) axis label text on any screen narrower than 800 CSS pixels, like a phone.
  // Measuring first keeps 1 user unit == 1 real pixel, so nothing gets stretched.
  const measuredWidth = Math.round(svg.getBoundingClientRect().width);
  const W = measuredWidth > 0 ? measuredWidth : 800, H = 260;
  const padL = 58, padR = 10, padT = 14, padB = 24;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = "";

  const metric = METRICS.find(m => m.field === metricSelect.value) || METRICS[0];
  const points = (currentHistory && currentHistory.points || [])
    .filter(p => p[metric.field] !== null && p[metric.field] !== undefined);

  if (!currentHistory || points.length < 2) {
    const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
    text.setAttribute("x", W / 2);
    text.setAttribute("y", H / 2);
    text.setAttribute("text-anchor", "middle");
    text.setAttribute("class", "chart-empty");
    text.textContent = "Not enough history yet for this view";
    svg.appendChild(text);
    return;
  }

  const xs = points.map(p => p.bucket_ts);
  const ys = points.map(p => p[metric.field]);
  const minX = xs[0], maxX = xs[xs.length - 1];
  // Y-axis always includes zero as its baseline - most fields here (current, voltage,
  // energy) are never negative anyway, but power/reactive-power fields can be, so pin
  // to zero rather than assuming, and only pad below it when there's actual negative
  // data to show.
  const dataMin = Math.min(0, ...ys), dataMax = Math.max(0, ...ys);
  const axis = niceAxisTicks(dataMin, dataMax, 5);
  const minY = axis.min, maxY = axis.max;

  const xScale = x => padL + (W - padL - padR) * (x - minX) / (maxX - minX);
  const yScale = y => padT + (H - padT - padB) * (1 - (y - minY) / (maxY - minY));

  const ns = "http://www.w3.org/2000/svg";
  const addEl = (tag, attrs) => {
    const el = document.createElementNS(ns, tag);
    for (const k in attrs) el.setAttribute(k, attrs[k]);
    svg.appendChild(el);
    return el;
  };

  // Horizontal gridlines + y-axis labels, at the "nice" step computed above - so the
  // gap between any two labels is always a round number (e.g. steps of 500, not 405.8).
  const tickCount = Math.round((axis.max - axis.min) / axis.step);
  for (let i = 0; i <= tickCount; i++) {
    const y = axis.min + i * axis.step;
    const yPix = yScale(y);
    addEl("line", { x1: padL, x2: W - padR, y1: yPix, y2: yPix, class: "chart-gridline" });
    addEl("text", { x: padL - 8, y: yPix + 4, "text-anchor": "end", class: "chart-axis-label" })
      .textContent = fmt(y, axis.decimals) + " " + metric.unit;
  }

  // x-axis labels (5 ticks).
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const x = minX + (maxX - minX) * i / ticks;
    addEl("text", { x: xScale(x), y: H - 4, "text-anchor": i === 0 ? "start" : (i === ticks ? "end" : "middle"), class: "chart-axis-label" })
      .textContent = rangeLabel(currentRange, x);
  }

  // Area + line.
  const linePath = points.map((p, i) => (i === 0 ? "M" : "L") + xScale(p.bucket_ts) + "," + yScale(p[metric.field])).join(" ");
  const areaPath = linePath + ` L${xScale(maxX)},${yScale(minY)} L${xScale(minX)},${yScale(minY)} Z`;
  addEl("path", { d: areaPath, class: "chart-area" });
  addEl("path", { d: linePath, class: "chart-line" });

  // Hover layer: an invisible full-height rect capturing mousemove, plus a
  // crosshair line and dot that get repositioned to the nearest point.
  const crosshair = addEl("line", { x1: 0, x2: 0, y1: padT, y2: H - padB, class: "chart-crosshair" });
  crosshair.style.display = "none";
  const dot = addEl("circle", { r: 4, class: "chart-dot" });
  dot.style.display = "none";
  const hitRect = addEl("rect", { x: padL, y: padT, width: W - padL - padR, height: H - padT - padB, fill: "transparent" });

  const tooltip = document.getElementById("chart-tooltip");
  const wrap = document.getElementById("chart-wrap");

  function pointerToIndex(clientX) {
    const rect = svg.getBoundingClientRect();
    const frac = (clientX - rect.left) / rect.width;
    const x = minX + (maxX - minX) * frac;
    let closest = 0, best = Infinity;
    for (let i = 0; i < points.length; i++) {
      const d = Math.abs(points[i].bucket_ts - x);
      if (d < best) { best = d; closest = i; }
    }
    return closest;
  }

  hitRect.addEventListener("mousemove", (ev) => {
    const i = pointerToIndex(ev.clientX);
    const p = points[i];
    const xPix = xScale(p.bucket_ts), yPix = yScale(p[metric.field]);
    crosshair.setAttribute("x1", xPix); crosshair.setAttribute("x2", xPix);
    crosshair.style.display = "";
    dot.setAttribute("cx", xPix); dot.setAttribute("cy", yPix);
    dot.style.display = "";

    const wrapRect = wrap.getBoundingClientRect();
    const svgRect = svg.getBoundingClientRect();
    tooltip.style.left = (svgRect.left - wrapRect.left + (xPix / W) * svgRect.width) + "px";
    tooltip.style.top = (svgRect.top - wrapRect.top + (yPix / H) * svgRect.height) + "px";
    tooltip.innerHTML = fmt(p[metric.field], metric.digits) + " " + metric.unit +
      '<br><span class="t-time">' + new Date(p.bucket_ts * 1000).toLocaleString() + "</span>";
    tooltip.hidden = false;
  });
  hitRect.addEventListener("mouseleave", () => {
    crosshair.style.display = "none";
    dot.style.display = "none";
    tooltip.hidden = true;
  });
}

metricSelect.addEventListener("change", drawChart);
document.getElementById("range-buttons").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-range]");
  if (!btn) return;
  for (const b of document.querySelectorAll("#range-buttons button")) b.classList.remove("active");
  btn.classList.add("active");
  currentRange = btn.dataset.range;
  loadHistory();
});

loadHistory();
setInterval(loadHistory, 60000);

// Redraw on resize/orientation-change so the viewBox keeps matching the real width
// (e.g. rotating a phone, or resizing a desktop window) - debounced since 'resize'
// fires continuously while dragging.
let resizeRedrawTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeRedrawTimer);
  resizeRedrawTimer = setTimeout(drawChart, 150);
});
</script>
</body>
</html>
"""

_DASHBOARD_HTML_BYTES = DASHBOARD_HTML.encode("utf-8")


class _DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "OsgpDashboard/1.0"

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, _DASHBOARD_HTML_BYTES, "text/html; charset=utf-8")
        elif parsed.path == "/api/live":
            body = json.dumps(self.server.reader.get_snapshot()).encode("utf-8")
            self._send(200, body, "application/json")
        elif parsed.path == "/api/history":
            self._handle_history(parse_qs(parsed.query))
        elif parsed.path == "/api/system":
            db_path = getattr(self.server, "history_db_path", None)
            self._send(200, json.dumps(get_system_stats(db_path=db_path)).encode("utf-8"),
                       "application/json")
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

    def _handle_history(self, query):
        db_path = getattr(self.server, "history_db_path", None)
        if not db_path:
            self._send(404, b'{"error": "history logging is disabled"}', "application/json")
            return
        range_name = query.get("range", ["24h"])[0]
        if range_name not in RANGE_PRESETS:
            self._send(400, b'{"error": "invalid range"}', "application/json")
            return
        try:
            result = query_range_preset(db_path, range_name)
        except sqlite3.Error as e:
            logger.warning("History query failed: %s", e)
            self._send(500, b'{"error": "query failed"}', "application/json")
            return
        self._send(200, json.dumps(result).encode("utf-8"), "application/json")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.debug("%s - %s", self.address_string(), fmt % args)


class DashboardServer:
    """Runs the HTTP server on a background daemon thread."""

    def __init__(self, reader, bind_address, port, history_db_path=None):
        self._httpd = ThreadingHTTPServer((bind_address, port), _DashboardRequestHandler)
        self._httpd.reader = reader
        self._httpd.history_db_path = history_db_path
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="Dashboard", daemon=True)

    def start(self):
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        logger.info("Dashboard listening on http://%s:%d/", host, port)

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
