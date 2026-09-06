import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import shelly_emulator as se  # noqa: E402


class FakeReader:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_snapshot(self):
        return self.snapshot


SAMPLE_SNAPSHOT = {
    "connected": True,
    "fwd_active_power_w": 2300.0, "rev_active_power_w": 0.0,
    "import_reactive_var": 0.0, "export_reactive_var": 500.0,
    "l1_current_a": 10.0, "l2_current_a": 3.0, "l3_current_a": 1.0,
    "l1_voltage_v": 230.1, "l2_voltage_v": 229.8, "l3_voltage_v": 231.0,
    "fwd_active_energy_wh": 5043527.0, "rev_active_energy_wh": 943342.0,
}

IDENTITY = {"mac": "AABBCCDDEEFF", "device_id": "shellypro3em-aabbccddeeff"}


class TestSplitByCurrent(unittest.TestCase):
    def test_even_split_with_no_current_data(self):
        self.assertEqual(se._split_by_current(300, [None, None, None]), [100.0, 100.0, 100.0])

    def test_weighted_split(self):
        self.assertEqual(se._split_by_current(300, [1, 2, 3]), [50.0, 100.0, 150.0])

    def test_zero_total_is_all_zero(self):
        self.assertEqual(se._split_by_current(0, [1, 1, 1]), [0.0, 0.0, 0.0])


class TestIdentity(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_generates_locally_administered_mac(self):
        identity = se._load_or_create_identity(self.path)
        mac_bytes = bytes.fromhex(identity["mac"])
        self.assertTrue(mac_bytes[0] & 0x02, "locally-administered bit must be set")
        self.assertFalse(mac_bytes[0] & 0x01, "multicast bit must be clear")
        self.assertEqual(identity["device_id"], "shellypro3em-%s" % identity["mac"].lower())

    def test_identity_is_stable_across_calls(self):
        first = se._load_or_create_identity(self.path)
        second = se._load_or_create_identity(self.path)
        self.assertEqual(first, second)


class TestDataMapper(unittest.TestCase):
    def setUp(self):
        self.mapper = se.ShellyDataMapper(FakeReader(SAMPLE_SNAPSHOT), IDENTITY)

    def test_device_info(self):
        info = self.mapper.device_info()
        self.assertEqual(info["gen"], 2)
        self.assertEqual(info["model"], se.MODEL)
        self.assertEqual(info["mac"], IDENTITY["mac"])
        self.assertFalse(info["auth_en"])

    def test_total_power_sign_convention_is_positive_for_import(self):
        em = self.mapper.em_get_status()
        self.assertEqual(em["total_act_power"], 2300.0)  # importing -> positive, matches Shelly

    def test_export_makes_total_power_negative(self):
        snap = dict(SAMPLE_SNAPSHOT, fwd_active_power_w=0.0, rev_active_power_w=800.0)
        mapper = se.ShellyDataMapper(FakeReader(snap), IDENTITY)
        em = mapper.em_get_status()
        self.assertEqual(em["total_act_power"], -800.0)

    def test_invert_power_sign_flips_total_and_per_phase(self):
        mapper = se.ShellyDataMapper(FakeReader(SAMPLE_SNAPSHOT), IDENTITY, invert_power_sign=True)
        em = mapper.em_get_status()
        self.assertEqual(em["total_act_power"], -2300.0)
        self.assertEqual(em["a_act_power"], -self.mapper.em_get_status()["a_act_power"])

    def test_invert_power_sign_does_not_affect_energy_counters(self):
        # Energy counters are separate non-negative imported/exported registers, not a
        # signed net value - there's nothing to invert there.
        mapper = se.ShellyDataMapper(FakeReader(SAMPLE_SNAPSHOT), IDENTITY, invert_power_sign=True)
        emdata = mapper.emdata_get_status()
        self.assertEqual(emdata["total_act"], SAMPLE_SNAPSHOT["fwd_active_energy_wh"])
        self.assertEqual(emdata["total_act_ret"], SAMPLE_SNAPSHOT["rev_active_energy_wh"])

    def test_per_phase_power_sums_to_total(self):
        em = self.mapper.em_get_status()
        total = em["a_act_power"] + em["b_act_power"] + em["c_act_power"]
        self.assertAlmostEqual(total, em["total_act_power"])

    def test_voltage_and_current_pass_through_unmodified(self):
        em = self.mapper.em_get_status()
        self.assertEqual(em["a_voltage"], 230.1)
        self.assertEqual(em["a_current"], 10.0)

    def test_neutral_current_is_null_not_fabricated(self):
        em = self.mapper.em_get_status()
        self.assertIsNone(em["n_current"])

    def test_emdata_totals_match_meter_energy_fields(self):
        emdata = self.mapper.emdata_get_status()
        self.assertEqual(emdata["total_act"], SAMPLE_SNAPSHOT["fwd_active_energy_wh"])
        self.assertEqual(emdata["total_act_ret"], SAMPLE_SNAPSHOT["rev_active_energy_wh"])

    def test_emdata_per_phase_sums_to_total(self):
        emdata = self.mapper.emdata_get_status()
        imported = sum(emdata["%s_total_act_energy" % p] for p in "abc")
        self.assertAlmostEqual(imported, emdata["total_act"])

    def test_dispatch_unknown_method_returns_none(self):
        self.assertIsNone(self.mapper.dispatch("Nonexistent.Method"))

    def test_dispatch_known_methods(self):
        self.assertIn("total_act_power", self.mapper.dispatch("EM.GetStatus"))
        self.assertIn("total_act", self.mapper.dispatch("EMData.GetStatus"))
        self.assertIn("model", self.mapper.dispatch("Shelly.GetDeviceInfo"))


class TestHttpServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.identity_path = tempfile.mkstemp()
        os.close(fd)
        os.remove(cls.identity_path)
        cls.server = se.ShellyEmulatorServer(FakeReader(SAMPLE_SNAPSHOT), "127.0.0.1", 0,
                                             identity_path=cls.identity_path)
        cls.server.start()
        cls.port = cls.server._httpd.server_address[1]
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        if os.path.exists(cls.identity_path):
            os.remove(cls.identity_path)

    def _get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path)) as r:
            return r.status, json.loads(r.read())

    def test_shelly_endpoint(self):
        status, body = self._get("/shelly")
        self.assertEqual(status, 200)
        self.assertEqual(body["gen"], 2)

    def test_rpc_em_get_status(self):
        status, body = self._get("/rpc/EM.GetStatus?id=0")
        self.assertEqual(status, 200)
        self.assertEqual(body["total_act_power"], 2300.0)

    def test_rpc_emdata_get_status(self):
        status, body = self._get("/rpc/EMData.GetStatus?id=0")
        self.assertEqual(status, 200)
        self.assertEqual(body["total_act"], 5043527.0)

    def test_rpc_query_string_form(self):
        status, body = self._get("/rpc?method=EM.GetStatus")
        self.assertEqual(status, 200)
        self.assertIn("total_act_power", body)

    def test_unknown_method_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/rpc/Bogus.Method")
        self.assertEqual(ctx.exception.code, 404)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)

    def test_post_rpc_envelope(self):
        payload = json.dumps({"id": 7, "src": "user_1", "method": "EM.GetStatus",
                              "params": {"id": 0}}).encode("utf-8")
        req = urllib.request.Request(
            "http://127.0.0.1:%d/rpc" % self.port, data=payload,
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            envelope = json.loads(r.read())
        self.assertEqual(envelope["id"], 7)
        self.assertEqual(envelope["dst"], "user_1")
        self.assertEqual(envelope["result"]["total_act_power"], 2300.0)


if __name__ == "__main__":
    unittest.main()
