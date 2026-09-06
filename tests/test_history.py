import os
import sys
import tempfile
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import history  # noqa: E402


def make_snapshot(value):
    return {field: float(value) for field in history.HISTORY_FIELDS}


class HistoryTestCase(unittest.TestCase):
    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        os.remove(self.db_path)  # init_db creates it fresh
        history.init_db(self.db_path)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            path = self.db_path + suffix
            if os.path.exists(path):
                os.remove(path)


class TestInsertAndQuery(HistoryTestCase):
    def test_insert_truncates_to_the_minute(self):
        history.insert_reading(self.db_path, make_snapshot(1), now=1_700_000_075)
        rows = history.query_history(self.db_path, 1_700_000_000, 1_700_000_120, 60)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["bucket_ts"], 1_700_000_040)

    def test_empty_snapshot_is_not_logged(self):
        wrote = history.insert_reading(self.db_path, {"connected": True, "last_update": "x"},
                                        now=1_700_000_000)
        self.assertFalse(wrote)
        self.assertEqual(history.query_history(self.db_path, 0, 2_000_000_000, 60), [])

    def test_bucket_averages(self):
        base = 1_700_000_000 // 120 * 120  # aligned to the 120s bucket size used below,
        # so consecutive minute-rows pair up predictably instead of straddling buckets.
        for i in range(10):
            history.insert_reading(self.db_path, make_snapshot(i), now=base + i * 60)
        buckets = history.query_history(self.db_path, base, base + 600, 120)
        self.assertEqual(len(buckets), 5)
        self.assertAlmostEqual(buckets[0]["fwd_active_power_w"], 0.5)
        self.assertAlmostEqual(buckets[-1]["fwd_active_power_w"], 8.5)

    def test_query_range_preset_unknown_name(self):
        self.assertIsNone(history.query_range_preset(self.db_path, "decade"))

    def test_query_range_preset_known_name(self):
        history.insert_reading(self.db_path, make_snapshot(1), now=1_700_000_000)
        result = history.query_range_preset(self.db_path, "24h", now=1_700_000_000 + 60)
        self.assertEqual(result["range"], "24h")
        self.assertEqual(result["bucket_seconds"], 300)
        self.assertEqual(len(result["points"]), 1)


class TestCoarsening(HistoryTestCase):
    def _insert_year(self, year, n_days, value=10.0):
        start = int(datetime(year, 1, 1).timestamp())
        for i in range(n_days * 24 * 60):
            history.insert_reading(self.db_path, make_snapshot(value), now=start + i * 60)

    def test_coarsen_year_reduces_resolution_and_keeps_average(self):
        self._insert_year(2025, n_days=2, value=10.0)  # 2*1440 = 2880 raw rows
        rows_after = history.coarsen_year(self.db_path, 2025)
        self.assertEqual(rows_after, 2880 // 5)
        start = int(datetime(2025, 1, 1).timestamp())
        end = int(datetime(2026, 1, 1).timestamp())
        rows = history.query_history(self.db_path, start, end, 300)
        self.assertEqual(len(rows), 2880 // 5)
        for row in rows:
            self.assertAlmostEqual(row["fwd_active_power_w"], 10.0)

    def test_coarsen_year_is_idempotent(self):
        self._insert_year(2025, n_days=1)
        first = history.coarsen_year(self.db_path, 2025)
        second = history.coarsen_year(self.db_path, 2025)
        self.assertEqual(first, second)

    def test_coarsen_year_with_no_data_is_a_noop(self):
        self.assertEqual(history.coarsen_year(self.db_path, 2025), 0)

    def test_maybe_coarsen_picks_year_minus_two(self):
        self._insert_year(2025, n_days=1)
        self._insert_year(2026, n_days=1)
        coarsened = history.maybe_coarsen_old_years(self.db_path, now=datetime(2027, 1, 1))
        self.assertEqual(coarsened, 2025)

        start_2025 = int(datetime(2025, 1, 1).timestamp())
        end_2025 = int(datetime(2026, 1, 1).timestamp())
        start_2026 = end_2025
        end_2026 = int(datetime(2027, 1, 1).timestamp())

        self.assertEqual(len(history.query_history(self.db_path, start_2025, end_2025, 300)),
                          1 * 1440 // 5)
        # 2026 must be untouched - still at 1-minute resolution.
        self.assertEqual(len(history.query_history(self.db_path, start_2026, end_2026, 60)),
                          1 * 1440)

    def test_maybe_coarsen_is_idempotent_across_calls(self):
        self._insert_year(2025, n_days=1)
        now = datetime(2027, 1, 1)
        first = history.maybe_coarsen_old_years(self.db_path, now=now)
        second = history.maybe_coarsen_old_years(self.db_path, now=now)
        self.assertEqual(first, 2025)
        self.assertIsNone(second)

    def test_maybe_coarsen_leaves_year_minus_one_untouched(self):
        # Only the year exactly two years back is eligible - a year that's merely
        # "previous" (one year back) must be left alone even though nothing else
        # exists yet for the actually-eligible year.
        self._insert_year(2026, n_days=1)
        history.maybe_coarsen_old_years(self.db_path, now=datetime(2027, 1, 1))
        start_2026 = int(datetime(2026, 1, 1).timestamp())
        end_2026 = int(datetime(2027, 1, 1).timestamp())
        self.assertEqual(len(history.query_history(self.db_path, start_2026, end_2026, 60)),
                          1 * 1440)

    def test_maybe_coarsen_on_empty_database_is_safe(self):
        result = history.maybe_coarsen_old_years(self.db_path, now=datetime(2027, 1, 1))
        self.assertEqual(result, 2025)  # marked as checked, even though there was nothing to do
        self.assertEqual(history.query_history(self.db_path, 0, 2_000_000_000, 3600), [])


if __name__ == "__main__":
    unittest.main()
