import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from shelly_emulator import ShellyDataMapper  # noqa: E402

try:
    import paho.mqtt.client  # noqa: F401
    HAVE_PAHO = True
except ImportError:
    HAVE_PAHO = False

if HAVE_PAHO:
    from shelly_mqtt_client import ShellyMqttClient  # noqa: E402


class FakeReader:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_snapshot(self):
        return self.snapshot


SAMPLE_SNAPSHOT = {
    "connected": True,
    "fwd_active_power_w": 150.0, "rev_active_power_w": 0.0,
    "l1_current_a": 1.15, "l2_current_a": 0.4, "l3_current_a": 0.6,
}
IDENTITY = {"mac": "AABBCCDDEEFF", "device_id": "shellypro3em-aabbccddeeff"}


@unittest.skipUnless(HAVE_PAHO, "paho-mqtt not installed - optional dependency")
class TestShellyMqttClient(unittest.TestCase):
    def setUp(self):
        mapper = ShellyDataMapper(FakeReader(SAMPLE_SNAPSHOT), IDENTITY)
        # Never actually connects - .start() is never called in these tests, only the
        # internal callbacks are invoked directly, with the real paho client swapped
        # out for a mock so no network access happens.
        self.client = ShellyMqttClient(mapper, "example.invalid:1883", None, None,
                                       topic_prefix="ecotestprefix")
        self.mock_mqtt = MagicMock()
        self.client._client = self.mock_mqtt
        self.client._host = "example.invalid"
        self.client._port = 1883

    def test_on_connect_success_publishes_online_and_announce_and_subscribes(self):
        self.client._on_connect(self.mock_mqtt, None, None, 0)

        calls = {c.args[0]: c for c in self.mock_mqtt.publish.call_args_list}
        self.assertIn("ecotestprefix/online", calls)
        self.assertEqual(calls["ecotestprefix/online"].kwargs.get("payload")
                         or calls["ecotestprefix/online"].args[1], "true")

        self.assertIn("ecotestprefix/announce", calls)
        announce_call = calls["ecotestprefix/announce"]
        announce_payload = announce_call.kwargs.get("payload") or announce_call.args[1]
        self.assertEqual(json.loads(announce_payload)["mac"], IDENTITY["mac"])

        self.mock_mqtt.subscribe.assert_called_once_with("ecotestprefix/rpc")

    def test_on_connect_failure_publishes_nothing(self):
        self.client._on_connect(self.mock_mqtt, None, None, 5)  # any non-zero reason code
        self.mock_mqtt.publish.assert_not_called()
        self.mock_mqtt.subscribe.assert_not_called()

    def test_on_message_dispatches_and_responds_to_src_topic(self):
        msg = MagicMock()
        msg.topic = "ecotestprefix/rpc"
        msg.payload = json.dumps({"id": 7, "src": "watcher_1", "method": "EM.GetStatus"}).encode()

        self.client._on_message(self.mock_mqtt, None, msg)

        self.mock_mqtt.publish.assert_called_once()
        call = self.mock_mqtt.publish.call_args
        self.assertEqual(call.args[0], "watcher_1/rpc")
        payload = call.kwargs.get("payload") or call.args[1]
        envelope = json.loads(payload)
        self.assertEqual(envelope["id"], 7)
        self.assertEqual(envelope["dst"], "watcher_1")
        self.assertEqual(envelope["result"]["total_act_power"], 150.0)

    def test_on_message_ignores_malformed_json(self):
        msg = MagicMock()
        msg.topic = "ecotestprefix/rpc"
        msg.payload = b"not json"
        self.client._on_message(self.mock_mqtt, None, msg)
        self.mock_mqtt.publish.assert_not_called()

    def test_on_message_unknown_method_publishes_nothing(self):
        msg = MagicMock()
        msg.topic = "ecotestprefix/rpc"
        msg.payload = json.dumps({"id": 1, "src": "watcher_1", "method": "Bogus.Method"}).encode()
        self.client._on_message(self.mock_mqtt, None, msg)
        self.mock_mqtt.publish.assert_not_called()

    def test_notify_loop_publishes_full_status_once(self):
        def stop_after_first_publish(*args, **kwargs):
            self.client._stop_requested.set()
        self.mock_mqtt.publish.side_effect = stop_after_first_publish

        self.client._notify_loop()

        self.mock_mqtt.publish.assert_called_once()
        call = self.mock_mqtt.publish.call_args
        self.assertEqual(call.args[0], "ecotestprefix/events/rpc")
        payload = call.kwargs.get("payload") or call.args[1]
        envelope = json.loads(payload)
        self.assertEqual(envelope["method"], "NotifyFullStatus")
        self.assertIn("em:0", envelope["params"])
        self.assertIn("ts", envelope["params"])

    def test_stop_publishes_offline_status(self):
        self.client.stop()
        self.mock_mqtt.publish.assert_called_once_with(
            "ecotestprefix/online", payload="false", qos=1, retain=True)
        self.mock_mqtt.loop_stop.assert_called_once()
        self.mock_mqtt.disconnect.assert_called_once()


class TestGracefulDegradation(unittest.TestCase):
    def test_missing_paho_mqtt_does_not_crash_construction(self):
        # Simulate the import failing, regardless of whether paho-mqtt happens to be
        # installed in this environment - mirrors main.py's own guarded import pattern.
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "paho.mqtt.client" or name.startswith("paho.mqtt.client"):
                raise ImportError("simulated missing paho-mqtt")
            return real_import(name, *args, **kwargs)

        import importlib
        import shelly_mqtt_client as module

        builtins.__import__ = fake_import
        try:
            importlib.reload(module)
            mapper = ShellyDataMapper(FakeReader(SAMPLE_SNAPSHOT), IDENTITY)
            client = module.ShellyMqttClient(mapper, "example.invalid:1883", None, None)
            self.assertIsNone(client._client)
            client.start()  # must be a no-op, not raise
            client.stop()   # must be a no-op, not raise
        finally:
            builtins.__import__ = real_import
            importlib.reload(module)  # restore normal behaviour for any later test


if __name__ == "__main__":
    unittest.main()
