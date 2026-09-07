"""Emulates an everHome EcoTracker's local HTTP API, so apps that support pairing one
as a generic "Smart CT" grid meter can use this Pi's OSGP readings instead - notably
Zendure's app, whose Hub/AIO/Hyper "Smart CT mode" supports an EcoTracker over local
Wi-Fi with a plain IP address, no cloud account (unlike its Shelly integration, which
requires logging into a real Shelly cloud account just to pair - see shelly_emulator.py
for that route, and the README for why this one is the simpler path for Zendure).

Endpoint (see everhome.cloud/en/developer/ecotracker):
  GET /v1/json - a single flat JSON object with instantaneous power and cumulative
  energy. Much simpler than Shelly's RPC schema: no device-identity handshake, no
  envelope, and - unlike Shelly - the sign convention is actually documented: positive
  power = consumption (importing from the grid), negative = feed-in (exporting).
  That matches what we already compute as fwd_active_power_w - rev_active_power_w, so
  no inversion/uncertainty here the way there was for Shelly.

Per-phase power (powerPhase1/2/3) is explicitly documented as optional - "may not be
provided by all meters" - so, same as the Shelly emulator, it's included as an estimate
(split proportionally by each phase's share of current, since the OSGP meter only
reports a combined total) rather than omitted, but it's worth remembering it's an
estimate, not a measurement, same caveat as before.
"""

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

logger = logging.getLogger("EcoTrackerEmulator")


def _split_by_current(total, currents):
    """Splits a combined reading across phases, weighted by each phase's share of
    total current - falls back to an even three-way split with no current data."""
    total = total or 0.0
    valid = [c or 0.0 for c in currents]
    current_sum = sum(valid)
    if current_sum <= 0:
        return [total / 3.0] * 3
    return [total * (c / current_sum) for c in valid]


class EcoTrackerDataMapper:
    def __init__(self, reader):
        self._reader = reader

    def get_reading(self):
        snap = self._reader.get_snapshot()
        fwd = snap.get("fwd_active_power_w") or 0.0
        rev = snap.get("rev_active_power_w") or 0.0
        power = fwd - rev  # positive = consumption/import, negative = feed-in/export

        currents = [snap.get("l1_current_a"), snap.get("l2_current_a"),
                    snap.get("l3_current_a")]
        phase_powers = _split_by_current(power, currents)

        return {
            "power": power,
            "powerAvg": power,  # no separate 1-minute average tracked here; same value
            "powerPhase1": phase_powers[0],
            "powerPhase2": phase_powers[1],
            "powerPhase3": phase_powers[2],
            "energyCounterIn": snap.get("fwd_active_energy_wh") or 0.0,
            "energyCounterOut": snap.get("rev_active_energy_wh") or 0.0,
        }


class _EcoTrackerRequestHandler(BaseHTTPRequestHandler):
    server_version = "EcoTracker/1.0"

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path == "/v1/json":
            body = json.dumps(self.server.mapper.get_reading()).encode("utf-8")
            self._send(200, body, "application/json")
        else:
            self._send(404, b'{"error": "not found"}', "application/json")

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.debug("%s - %s", self.address_string(), fmt % args)


class EcoTrackerEmulatorServer:
    """Runs the emulated EcoTracker HTTP API on a background daemon thread."""

    def __init__(self, reader, bind_address, port):
        self._httpd = ThreadingHTTPServer((bind_address, port), _EcoTrackerRequestHandler)
        self._httpd.mapper = EcoTrackerDataMapper(reader)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="EcoTrackerEmulator", daemon=True)

    def start(self):
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        logger.info("EcoTracker emulator listening on http://%s:%d/v1/json", host, port)

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
