"""Emulates a Shelly Pro 3EM's local HTTP API so battery/inverter apps that only
support pairing a real Shelly meter (Anker SOLIX, Marstek Venus, EcoFlow PowerStream,
Hoymiles/Growatt balcony inverters, etc.) can be pointed at this Pi instead, using the
OSGP meter's live readings as the "grid meter" input.

Endpoints (see shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/EM and .../EMData):
  GET /shelly                     - device identity, so the app recognizes a Pro 3EM
  GET /rpc/Shelly.GetDeviceInfo   - alias of the above, Gen2-flavored
  GET /rpc/EM.GetStatus?id=0      - live per-phase + total power/voltage/current
  GET /rpc/EMData.GetStatus?id=0  - cumulative imported/exported energy (Wh)
  POST /rpc                       - same methods, Shelly's src/dst RPC envelope

Sign convention (matches Shelly): positive power = importing from grid, negative =
exporting. Real per-phase current/voltage come straight from the meter (L1/L2/L3);
real per-phase POWER does not exist in the OSGP data (only a combined total), so it's
estimated by splitting the total proportionally to each phase's share of total current
- clearly an estimate, not a measurement, but the only thing derivable from what the
meter actually reports. Grid frequency isn't measured either; assumes 50 Hz (EU).

Deliberately its own small HTTP server on its own (unprivileged) port rather than
routes bolted onto the dashboard's - see README for how the real port 80 these apps
expect gets there via an iptables redirect set up by service.sh, not by this process
binding a privileged port itself.
"""

import json
import logging
import os
import random
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

logger = logging.getLogger("ShellyEmulator")

GRID_FREQUENCY_HZ = 50.0
MODEL = "SPEM-003CEBEU"  # real Shelly Pro 3EM model code
FIRMWARE_VERSION = "1.4.4"


def _load_or_create_identity(path):
    """A stable fake MAC/device-id, persisted so re-pairing isn't needed after every
    restart. Generated with the locally-administered bit set (and multicast bit
    cleared) so it can't collide with a real vendor-assigned MAC."""
    try:
        with open(path) as f:
            identity = json.load(f)
            if "mac" in identity and "device_id" in identity:
                return identity
    except (OSError, ValueError):
        pass

    rng = random.SystemRandom()
    octets = [rng.randint(0, 255) for _ in range(6)]
    octets[0] = (octets[0] & 0xFC) | 0x02  # locally administered, unicast
    mac = "".join("%02X" % b for b in octets)
    identity = {"mac": mac, "device_id": "shellypro3em-%s" % mac.lower()}
    try:
        with open(path, "w") as f:
            json.dump(identity, f)
    except OSError as e:
        logger.warning("Could not persist Shelly identity to %s: %s", path, e)
    return identity


def _split_by_current(total, currents):
    """Splits a combined reading across phases, weighted by each phase's share of
    total current - the closest approximation available without real per-phase power,
    falling back to an even three-way split if no current data is available at all."""
    total = total or 0.0
    valid = [c or 0.0 for c in currents]
    current_sum = sum(valid)
    if current_sum <= 0:
        return [total / 3.0] * 3
    return [total * (c / current_sum) for c in valid]


class ShellyDataMapper:
    """Reshapes an OsgpMeterReader.get_snapshot() dict into Shelly Pro 3EM JSON."""

    def __init__(self, reader, identity, invert_power_sign=False):
        self._reader = reader
        self._identity = identity
        # Shelly's own docs don't actually define the sign convention (confirmed by
        # reading them directly) - it's a physical CT-clamp-orientation convention,
        # not a protocol guarantee, which is exactly why real Shelly devices ship a
        # "Reverse CT measurement direction" toggle. This mirrors that: a config
        # change instead of a code change if a given battery app's assumption turns
        # out to be the opposite of what we picked (positive = importing).
        self._invert_power_sign = invert_power_sign

    def device_info(self):
        return {
            "id": self._identity["device_id"],
            "mac": self._identity["mac"],
            "model": MODEL,
            "gen": 2,
            "fw_id": "20250101-000000/v%s" % FIRMWARE_VERSION,
            "ver": FIRMWARE_VERSION,
            "app": "Pro3EM",
            "auth_en": False,
            "auth_domain": None,
        }

    def em_get_status(self):
        snap = self._reader.get_snapshot()
        fwd = snap.get("fwd_active_power_w") or 0.0
        rev = snap.get("rev_active_power_w") or 0.0
        total_power = fwd - rev  # positive = import, negative = export
        if self._invert_power_sign:
            total_power = -total_power

        currents = [snap.get("l1_current_a"), snap.get("l2_current_a"),
                    snap.get("l3_current_a")]
        voltages = [snap.get("l1_voltage_v"), snap.get("l2_voltage_v"),
                    snap.get("l3_voltage_v")]
        powers = _split_by_current(total_power, currents)

        result = {"id": 0}
        for phase, power, current, voltage in zip("abc", powers, currents, voltages):
            result["%s_current" % phase] = current
            result["%s_voltage" % phase] = voltage
            result["%s_act_power" % phase] = power
            # No per-phase reactive breakdown to derive a real power factor from -
            # assume unity (pf=1.0), so apparent power reduces to |active power|.
            result["%s_aprt_power" % phase] = abs(power)
            result["%s_pf" % phase] = 1.0
            result["%s_freq" % phase] = GRID_FREQUENCY_HZ
            result["%s_errors" % phase] = []

        result["total_current"] = sum(c or 0.0 for c in currents)
        result["total_act_power"] = total_power
        result["total_aprt_power"] = sum(result["%s_aprt_power" % p] for p in "abc")
        result["n_current"] = None  # neutral current isn't measured by this meter
        result["user_calibrated_phase"] = []
        result["errors"] = []
        return result

    def emdata_get_status(self):
        snap = self._reader.get_snapshot()
        total_act = snap.get("fwd_active_energy_wh") or 0.0
        total_act_ret = snap.get("rev_active_energy_wh") or 0.0

        currents = [snap.get("l1_current_a"), snap.get("l2_current_a"),
                    snap.get("l3_current_a")]
        imported = _split_by_current(total_act, currents)
        exported = _split_by_current(total_act_ret, currents)

        result = {"id": 0}
        for phase, imp, exp in zip("abc", imported, exported):
            result["%s_total_act_energy" % phase] = imp
            result["%s_total_act_ret_energy" % phase] = exp
        result["total_act"] = total_act
        result["total_act_ret"] = total_act_ret
        return result

    def dispatch(self, method):
        if method in ("EM.GetStatus", "EM.GetConfig"):
            return self.em_get_status()
        if method == "EMData.GetStatus":
            return self.emdata_get_status()
        if method in ("Shelly.GetDeviceInfo", "Shelly.GetStatus"):
            return self.device_info()
        return None


class _ShellyRequestHandler(BaseHTTPRequestHandler):
    server_version = "ShellyPro3EM/%s" % FIRMWARE_VERSION

    def do_GET(self):
        parsed = urlsplit(self.path)
        mapper = self.server.mapper
        if parsed.path == "/shelly":
            self._send_json(200, mapper.device_info())
        elif parsed.path.startswith("/rpc/"):
            method = parsed.path[len("/rpc/"):]
            self._respond_to_method(method)
        elif parsed.path == "/rpc":
            method = parse_qs(parsed.query).get("method", [None])[0]
            self._respond_to_method(method)
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        mapper = self.server.mapper
        if self.path != "/rpc":
            self._send_json(404, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length)) if length else {}
        except (ValueError, json.JSONDecodeError):
            body = {}
        method = body.get("method")
        result = mapper.dispatch(method) if method else None
        if result is None:
            self._send_json(404, {"error": "unknown method %r" % method})
            return
        envelope = {
            "id": body.get("id", 0),
            "src": mapper.device_info()["id"],
            "dst": body.get("src"),
            "result": result,
        }
        self._send_json(200, envelope)

    def _respond_to_method(self, method):
        mapper = self.server.mapper
        result = mapper.dispatch(method) if method else None
        if result is None:
            self._send_json(404, {"error": "unknown method %r" % method})
            return
        # Gen2's GET shorthand returns the result object directly, unlike POST /rpc's
        # src/dst envelope.
        self._send_json(200, result)

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        logger.debug("%s - %s", self.address_string(), fmt % args)


class ShellyEmulatorServer:
    """Runs the emulated Shelly Pro 3EM HTTP API on a background daemon thread."""

    def __init__(self, reader, bind_address, port, identity_path="shelly_identity.json",
                invert_power_sign=False):
        identity = _load_or_create_identity(identity_path)
        self._httpd = ThreadingHTTPServer((bind_address, port), _ShellyRequestHandler)
        self._httpd.mapper = ShellyDataMapper(reader, identity, invert_power_sign)
        self._identity = identity
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="ShellyEmulator", daemon=True)

    def start(self):
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        logger.info("Shelly Pro 3EM emulator listening on http://%s:%d/ (id=%s, mac=%s) "
                    "- pair the battery app to port 80 on this host, not %d directly, "
                    "if the port-80 redirect from service.sh is set up",
                    host, port, self._identity["device_id"], self._identity["mac"], port)

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
