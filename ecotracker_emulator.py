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

Discoverability: apps that "discover" a Smart CT meter rather than take a manual IP
(confirmed necessary - the plain HTTP endpoint alone isn't enough) rely on mDNS/DNS-SD:
a service of type "_everhome._tcp" named "ecotracker-<MAC>", where real EcoTracker
devices' MAC addresses start with the vendor OUI B4:3A:45 - matching that OUI, rather
than a random locally-administered MAC, follows the convention used by the existing
open-source EcoTracker emulators this was checked against (see README). This needs the
'zeroconf' package (pip install zeroconf) - a small, well-established pure-Python mDNS
implementation - since hand-rolling raw mDNS/DNS-SD packet encoding from scratch would
be a much bigger and more fragile undertaking than this one opt-in feature warrants.
Only imported if EcoTracker emulation is actually enabled; the rest of this tool has no
dependency beyond pyserial.
"""

import json
import logging
import random
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

logger = logging.getLogger("EcoTrackerEmulator")

ECOTRACKER_OUI = "B43A45"  # real everHome EcoTracker vendor MAC prefix


def _load_or_create_mac(path):
    """A stable fake MAC using the real EcoTracker OUI (see module docstring),
    persisted so re-pairing/re-discovery isn't needed after a restart."""
    try:
        with open(path) as f:
            data = json.load(f)
            if "mac" in data:
                return data["mac"]
    except (OSError, ValueError):
        pass
    rng = random.SystemRandom()
    suffix = "".join("%02X" % rng.randint(0, 255) for _ in range(3))
    mac = ECOTRACKER_OUI + suffix
    try:
        with open(path, "w") as f:
            json.dump({"mac": mac}, f)
    except OSError as e:
        logger.warning("Could not persist EcoTracker identity to %s: %s", path, e)
    return mac


def _get_local_ip():
    """Best-effort outbound local IP - mDNS needs a concrete address to advertise, not
    the 0.0.0.0 wildcard the HTTP server itself is happy to bind to."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # UDP 'connect' doesn't send packets, just picks a route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def _register_mdns(bind_address, port, mac):
    try:
        from zeroconf import ServiceInfo, Zeroconf
    except ImportError:
        logger.error("The 'zeroconf' package is required for EcoTracker network discovery "
                     "(pip install zeroconf, or add it to requirements.txt) - the plain "
                     "HTTP endpoint still works for a manual-IP pairing flow, but an app "
                     "that 'discovers' a Smart CT meter rather than taking an IP won't find "
                     "this one without it.")
        return None

    advertise_ip = bind_address if bind_address not in ("0.0.0.0", "::") else _get_local_ip()
    instance_name = "ecotracker-%s" % mac
    info = ServiceInfo(
        "_everhome._tcp.local.",
        "%s._everhome._tcp.local." % instance_name,
        addresses=[socket.inet_aton(advertise_ip)],
        port=port,
        properties={"serial": mac, "productid": "ECOTRACKER", "ip": advertise_ip},
        server="%s.local." % instance_name,
    )
    zc = Zeroconf()
    zc.register_service(info)
    logger.info("Announcing via mDNS as '%s' (_everhome._tcp) at %s:%d",
               instance_name, advertise_ip, port)
    return zc


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
    """Runs the emulated EcoTracker HTTP API on a background daemon thread, and
    announces it via mDNS so 'discover a Smart CT meter' flows can find it."""

    def __init__(self, reader, bind_address, port, identity_path="ecotracker_identity.json"):
        self._httpd = ThreadingHTTPServer((bind_address, port), _EcoTrackerRequestHandler)
        self._httpd.mapper = EcoTrackerDataMapper(reader)
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        name="EcoTrackerEmulator", daemon=True)
        self._mac = _load_or_create_mac(identity_path)
        self._bind_address = bind_address
        self._zeroconf = None

    def start(self):
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        logger.info("EcoTracker emulator listening on http://%s:%d/v1/json", host, port)
        self._zeroconf = _register_mdns(self._bind_address, port, self._mac)

    def stop(self):
        if self._zeroconf is not None:
            self._zeroconf.unregister_all_services()
            self._zeroconf.close()
        self._httpd.shutdown()
        self._httpd.server_close()
