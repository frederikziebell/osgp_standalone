"""Publishes this Pi's Shelly Pro 3EM emulation over MQTT, for integrations that add a
"Shelly" device configured to connect to *their own* MQTT broker rather than relying on
local HTTP discovery or Shelly's own cloud - notably everHome, whose app lets you add a
Shelly and shows an MQTT username/password to enter into the Shelly's own MQTT settings,
with the broker at everhome.cloud:1883. This sidesteps both problems hit by the other
routes in this project: EcoTracker's BLE-first pairing (see ecotracker_emulator.py on
the ecotracker_emulation branch) and Shelly's own cloud device-registration check (see
the "Shelly.GetConfig" work earlier on this branch) - everHome's broker is its own, not
Shelly's, so there's no "is this really Shelly-manufactured hardware" check to fail.

Real Shelly Gen2 devices, once configured via Mqtt.SetConfig, behave as an MQTT client
that (see shelly-api-docs.shelly.cloud/gen2/ComponentsAndServices/Mqtt/ and
.../General/RPCChannels/):
  - connects to the configured broker with the given user/pass
  - sets a Last-Will-and-Testament of "<topic_prefix>/online" = "false" (retained), so
    the broker itself announces "offline" if the connection drops uncleanly - almost
    certainly how an integration like everHome determines online/offline status, unlike
    the local-HTTP routes where that turned out to depend on genuine Shelly-cloud state
  - publishes "<topic_prefix>/online" = "true" (retained) once connected
  - publishes "<topic_prefix>/announce" once, with device identity
  - subscribes to "<topic_prefix>/rpc" for incoming RPC requests, answered the exact
    same way as the HTTP POST /rpc endpoint (same ShellyDataMapper.dispatch()),
    publishing the response to "<src>/rpc" per the request's own "src" field
  - periodically publishes "<topic_prefix>/events/rpc" with a NotifyFullStatus message
    carrying the full device status (same content as GET /rpc/Shelly.GetStatus)

topic_prefix defaults to the device id, matching a real device's own default, but is
independently configurable since an MQTT broker's ACLs (including everHome's own, most
likely) are commonly scoped to a specific prefix tied to the username/credentials
issued for that device entry - use whatever prefix the integration's setup screen
specifies, if it specifies one, rather than assuming it matches our own generated id.

Uses the 'paho-mqtt' package (pip install paho-mqtt) - the de facto standard Python
MQTT client - only needed if this feature is enabled, same as 'zeroconf' for EcoTracker
discovery: not a dependency of the rest of this tool.
"""

import json
import logging
import threading
import time

logger = logging.getLogger("ShellyMqttClient")

DEFAULT_NOTIFY_INTERVAL_SECONDS = 5
DEFAULT_PORT_PLAIN = 1883
DEFAULT_PORT_TLS = 8883


class ShellyMqttClient:
    """Wraps a paho-mqtt client publishing/answering as the given ShellyDataMapper."""

    def __init__(self, mapper, server, username, password, topic_prefix=None,
                use_ssl=False, notify_interval_seconds=DEFAULT_NOTIFY_INTERVAL_SECONDS):
        self._mapper = mapper
        self._topic_prefix = topic_prefix or mapper.device_info()["id"]
        self._notify_interval = (notify_interval_seconds if notify_interval_seconds > 0
                                 else DEFAULT_NOTIFY_INTERVAL_SECONDS)
        self._stop_requested = threading.Event()
        self._notify_thread = None
        self._client = None

        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            logger.error("The 'paho-mqtt' package is required for Shelly-over-MQTT "
                        "(pip install paho-mqtt) - this feature is disabled without it.")
            return

        host, _, port_str = server.partition(":")
        port = int(port_str) if port_str else (DEFAULT_PORT_TLS if use_ssl else DEFAULT_PORT_PLAIN)

        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=self._topic_prefix)
        if username:
            client.username_pw_set(username, password)
        if use_ssl:
            client.tls_set()
        client.will_set("%s/online" % self._topic_prefix, payload="false", qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_message = self._on_message
        self._client = client
        self._host = host
        self._port = port

    def start(self):
        if self._client is None:
            return
        self._client.connect_async(self._host, self._port, keepalive=60)
        self._client.loop_start()
        self._notify_thread = threading.Thread(target=self._notify_loop,
                                               name="ShellyMqttNotify", daemon=True)
        self._notify_thread.start()

    def stop(self):
        self._stop_requested.set()
        if self._notify_thread is not None:
            self._notify_thread.join(timeout=5.0)
        if self._client is not None:
            # A clean shutdown too, not just relying on the LWT (which only fires on an
            # *unclean* disconnect - publishing this ourselves covers a graceful stop).
            try:
                self._client.publish("%s/online" % self._topic_prefix,
                                     payload="false", qos=1, retain=True)
            except Exception as e:
                logger.debug("Could not publish offline status on shutdown: %s", e)
            self._client.loop_stop()
            self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            logger.error("MQTT connect failed: %s", reason_code)
            return
        logger.info("Connected to MQTT broker %s:%d, publishing as '%s'",
                   self._host, self._port, self._topic_prefix)
        self._client.publish("%s/online" % self._topic_prefix,
                             payload="true", qos=1, retain=True)
        self._client.publish("%s/announce" % self._topic_prefix,
                             payload=json.dumps(self._mapper.device_info()), qos=1)
        self._client.subscribe("%s/rpc" % self._topic_prefix)

    def _on_message(self, client, userdata, msg):
        try:
            body = json.loads(msg.payload)
        except (ValueError, UnicodeDecodeError):
            logger.warning("Ignoring malformed MQTT RPC message on %s", msg.topic)
            return
        method = body.get("method")
        src = body.get("src")
        logger.debug("MQTT RPC method=%r body=%r", method, body)
        result = self._mapper.dispatch(method, body.get("params")) if method else None
        if result is None or not src:
            return
        envelope = {"id": body.get("id", 0), "src": self._topic_prefix, "dst": src,
                   "result": result}
        self._client.publish("%s/rpc" % src, payload=json.dumps(envelope), qos=1)

    def _notify_loop(self):
        while not self._stop_requested.is_set():
            try:
                status = self._mapper.shelly_get_status()
                status["ts"] = time.time()
                envelope = {
                    "src": self._topic_prefix,
                    "dst": "%s/events" % self._topic_prefix,
                    "method": "NotifyFullStatus",
                    "params": status,
                }
                self._client.publish("%s/events/rpc" % self._topic_prefix,
                                     payload=json.dumps(envelope), qos=0)
            except Exception as e:
                logger.warning("Failed to publish MQTT status notification: %s", e)
            self._stop_requested.wait(self._notify_interval)
