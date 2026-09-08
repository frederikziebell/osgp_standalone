import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ecotracker_emulator as ee  # noqa: E402

try:
    import zeroconf  # noqa: F401
    HAVE_ZEROCONF = True
except ImportError:
    HAVE_ZEROCONF = False


class FakeReader:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def get_snapshot(self):
        return self.snapshot


SAMPLE_SNAPSHOT = {
    "connected": True,
    "fwd_active_power_w": 107.0, "rev_active_power_w": 0.0,
    "l1_current_a": 1.14, "l2_current_a": 0.22, "l3_current_a": 0.6,
    "fwd_active_energy_wh": 5057500.0, "rev_active_energy_wh": 944183.0,
}


class TestSplitByCurrent(unittest.TestCase):
    def test_even_split_with_no_current_data(self):
        self.assertEqual(ee._split_by_current(300, [None, None, None]), [100.0, 100.0, 100.0])

    def test_weighted_split(self):
        self.assertEqual(ee._split_by_current(300, [1, 2, 3]), [50.0, 100.0, 150.0])

    def test_zero_total_is_all_zero(self):
        self.assertEqual(ee._split_by_current(0, [1, 1, 1]), [0.0, 0.0, 0.0])


class TestIdentity(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_mac_uses_real_ecotracker_oui(self):
        identity = ee._load_or_create_identity(self.path)
        self.assertTrue(identity["mac"].startswith(ee.ECOTRACKER_OUI))

    def test_mac_and_serial_are_independent(self):
        # Matches wwerther/ha-ecotracker-emulator's config flow, which keeps these as
        # two separate random values rather than reusing one for both.
        identity = ee._load_or_create_identity(self.path)
        self.assertNotEqual(identity["mac"], identity["serial"])
        self.assertFalse(identity["serial"].upper().startswith(ee.ECOTRACKER_OUI))

    def test_identity_is_stable_across_calls(self):
        first = ee._load_or_create_identity(self.path)
        second = ee._load_or_create_identity(self.path)
        self.assertEqual(first, second)


class TestDataMapper(unittest.TestCase):
    def setUp(self):
        self.mapper = ee.EcoTrackerDataMapper(FakeReader(SAMPLE_SNAPSHOT))

    def test_power_sign_positive_for_consumption(self):
        reading = self.mapper.get_reading()
        self.assertEqual(reading["power"], 107)  # importing -> positive, per EcoTracker's own docs

    def test_export_makes_power_negative(self):
        snap = dict(SAMPLE_SNAPSHOT, fwd_active_power_w=0.0, rev_active_power_w=500.0)
        mapper = ee.EcoTrackerDataMapper(FakeReader(snap))
        self.assertEqual(mapper.get_reading()["power"], -500)

    def test_values_are_plain_integers_not_floats(self):
        # The real device's documented example uses integers ("power": 125, not
        # 125.0) - a strict client-side parser could care about the distinction.
        reading = self.mapper.get_reading()
        for key in ("power", "powerAvg", "powerPhase1", "powerPhase2", "powerPhase3",
                    "energyCounterIn", "energyCounterOut"):
            self.assertIsInstance(reading[key], int, key)

    def test_phase_powers_sum_to_total(self):
        reading = self.mapper.get_reading()
        total = reading["powerPhase1"] + reading["powerPhase2"] + reading["powerPhase3"]
        self.assertAlmostEqual(total, reading["power"], delta=1)

    def test_energy_counters_match_meter_fields(self):
        reading = self.mapper.get_reading()
        self.assertEqual(reading["energyCounterIn"], int(SAMPLE_SNAPSHOT["fwd_active_energy_wh"]))
        self.assertEqual(reading["energyCounterOut"], int(SAMPLE_SNAPSHOT["rev_active_energy_wh"]))


class TestHttpServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.identity_path = tempfile.mkstemp()
        os.close(fd)
        os.remove(cls.identity_path)
        cls.server = ee.EcoTrackerEmulatorServer(FakeReader(SAMPLE_SNAPSHOT), "127.0.0.1", 0,
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

    def test_v1_json_endpoint(self):
        status, body = self._get("/v1/json")
        self.assertEqual(status, 200)
        self.assertEqual(body["power"], 107)

    def test_unknown_path_is_404(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get("/nope")
        self.assertEqual(ctx.exception.code, 404)


@unittest.skipUnless(HAVE_ZEROCONF, "zeroconf not installed - optional dependency")
class TestMdns(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp()
        os.close(fd)
        os.remove(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_register_and_self_query(self):
        identity = ee._load_or_create_identity(self.path)
        zc = ee._register_mdns("127.0.0.1", 8395, identity)
        try:
            info = zc.get_service_info(
                "_everhome._tcp.local.",
                "ecotracker-%s._everhome._tcp.local." % identity["mac"])
            self.assertIsNotNone(info)
            props = {k.decode(): v.decode() for k, v in info.properties.items()}
            self.assertEqual(props["productid"], ee.ECOTRACKER_PRODUCT_ID)
            self.assertEqual(props["serial"], identity["serial"])
        finally:
            zc.close()


if __name__ == "__main__":
    unittest.main()
