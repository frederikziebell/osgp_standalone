"""Built-in LAN dashboard: a stdlib-only HTTP server exposing the meter reader's live
readings as JSON, plus a static HTML page that polls them.

No third-party dependencies (Flask, etc.) - just http.server, so it costs nothing extra
to run on a Pi. Not authenticated: anyone on the LAN that can reach the bound
address/port can view live readings. Keep it off a network you don't trust, or bind it
to localhost and reach it over an SSH tunnel / VPN instead.
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
  .status { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-2); }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--muted); flex: none; }
  .dot.good { background: var(--good); }
  .dot.critical { background: var(--critical); }
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
  .tile {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 14px 16px;
  }
  .tile .label { font-size: 13px; color: var(--text-2); margin-bottom: 4px; }
  .tile .value { font-size: 22px; font-weight: 600; }
  .tile .value .unit { font-size: 13px; font-weight: 500; color: var(--text-2); margin-left: 4px; }
  .section-title { font-size: 13px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; margin: 24px 0 10px; }
  footer { margin-top: 24px; font-size: 12px; color: var(--muted); }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Smart Meter Live Dashboard</h1>
    <div class="status"><span class="dot" id="dot"></span><span id="status-text">Loading...</span></div>
  </header>

  <div class="hero">
    <div class="label">Forward active power</div>
    <div class="value" id="hero-power">&#8211;<span class="unit">W</span></div>
  </div>

  <div class="section-title">Power</div>
  <div class="grid">
    <div class="tile"><div class="label">Reverse active power</div><div class="value" id="rev-power">&#8211;<span class="unit">W</span></div></div>
    <div class="tile"><div class="label">Import reactive power</div><div class="value" id="import-var">&#8211;<span class="unit">VAr</span></div></div>
    <div class="tile"><div class="label">Export reactive power</div><div class="value" id="export-var">&#8211;<span class="unit">VAr</span></div></div>
  </div>

  <div class="section-title">Current</div>
  <div class="grid">
    <div class="tile"><div class="label">L1</div><div class="value" id="l1-a">&#8211;<span class="unit">A</span></div></div>
    <div class="tile"><div class="label">L2</div><div class="value" id="l2-a">&#8211;<span class="unit">A</span></div></div>
    <div class="tile"><div class="label">L3</div><div class="value" id="l3-a">&#8211;<span class="unit">A</span></div></div>
  </div>

  <div class="section-title">Voltage</div>
  <div class="grid">
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
</script>
</body>
</html>
"""

_DASHBOARD_HTML_BYTES = DASHBOARD_HTML.encode("utf-8")


class _DashboardRequestHandler(BaseHTTPRequestHandler):
    server_version = "OsgpDashboard/1.0"

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, _DASHBOARD_HTML_BYTES, "text/html; charset=utf-8")
        elif self.path == "/api/live":
            body = json.dumps(self.server.reader.get_snapshot()).encode("utf-8")
            self._send(200, body, "application/json")
        else:
            self._send(404, b"Not found", "text/plain; charset=utf-8")

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

    def __init__(self, reader, bind_address, port):
        self._httpd = ThreadingHTTPServer((bind_address, port), _DashboardRequestHandler)
        self._httpd.reader = reader
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="Dashboard", daemon=True)

    def start(self):
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        logger.info("Dashboard listening on http://%s:%d/", host, port)

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
