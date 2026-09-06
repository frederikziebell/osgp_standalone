"""SQLite-backed history logging for the live meter readings.

Design (see README for the full rationale):
 - One row per minute (decoupled from the meter's ~2s poll rate), keyed by unix epoch
   truncated to the minute. At that rate a year is ~525k rows / ~60MB - trivial for
   SQLite, and indexed range queries stay fast regardless of how much history has
   accumulated in total, since only the requested time window is ever scanned.
 - Chart views (24h/week/month/year) are answered by an on-the-fly bucketed SQL
   GROUP BY rather than pre-computed rollup tables - see RANGE_PRESETS.
 - Once a calendar year is more than a year in the past (i.e. at the start of each new
   year, the year before last), its raw 1-minute rows are consolidated down to 5-minute
   averages in place. This is a one-way, one-time-per-year operation tracked in
   coarsen_log so it's safe to check on every startup/day without redoing work.
"""

import logging
import sqlite3
import threading
import time
from datetime import date, datetime

logger = logging.getLogger("History")

# Keep in sync with the keys OsgpMeterReader.get_snapshot() populates from Table 28/23 -
# these are the only fields actually persisted (not 'connected'/'last_update' bookkeeping).
HISTORY_FIELDS = [
    "fwd_active_power_w",
    "rev_active_power_w",
    "import_reactive_var",
    "export_reactive_var",
    "l1_current_a",
    "l2_current_a",
    "l3_current_a",
    "l1_voltage_v",
    "l2_voltage_v",
    "l3_voltage_v",
    "fwd_active_energy_wh",
    "rev_active_energy_wh",
]

# name -> (lookback seconds, bucket size seconds). Bucket sizes are chosen so each
# view stays in the low hundreds of points (fast to query, cheap to draw, still
# detailed enough to be useful) - see the benchmark in the project history for the
# actual query-time numbers these were picked against.
RANGE_PRESETS = {
    "24h": (24 * 3600, 300),
    "week": (7 * 24 * 3600, 900),
    "month": (30 * 24 * 3600, 3600),
    "year": (365 * 24 * 3600, 86400),
}

# How far back a coarsened year stays consolidated into, once it's eligible.
COARSEN_BUCKET_SECONDS = 300

_COLUMNS_SQL = ", ".join("%s REAL" % f for f in HISTORY_FIELDS)
_FIELD_LIST_SQL = ", ".join(HISTORY_FIELDS)
_AVG_LIST_SQL = ", ".join("AVG(%s) AS %s" % (f, f) for f in HISTORY_FIELDS)
_INSERT_PLACEHOLDERS_SQL = ", ".join("?" * (len(HISTORY_FIELDS) + 1))


def _connect(db_path):
    con = sqlite3.connect(db_path, timeout=5.0)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init_db(db_path):
    con = _connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS readings (ts INTEGER PRIMARY KEY, %s)" % _COLUMNS_SQL
        )
        con.execute(
            "CREATE TABLE IF NOT EXISTS coarsen_log ("
            "year INTEGER PRIMARY KEY, coarsened_at TEXT NOT NULL, rows_after INTEGER NOT NULL)"
        )
        con.commit()
    finally:
        con.close()


def insert_reading(db_path, snapshot, now=None):
    """Logs one minute-resolution sample from a reader.get_snapshot() dict.

    Does nothing if the snapshot has no readings yet (e.g. before the first
    successful table28/23 read). Returns whether a row was written.
    """
    values = [snapshot.get(field) for field in HISTORY_FIELDS]
    if all(value is None for value in values):
        return False
    ts = int((now if now is not None else time.time()) // 60) * 60
    con = _connect(db_path)
    try:
        with con:
            con.execute(
                "INSERT OR REPLACE INTO readings (ts, %s) VALUES (%s)"
                % (_FIELD_LIST_SQL, _INSERT_PLACEHOLDERS_SQL),
                [ts] + values,
            )
    finally:
        con.close()
    return True


def query_history(db_path, since_ts, until_ts, bucket_seconds):
    """Returns a list of {'ts': bucket_start, field: avg_value, ...} dicts, one per
    non-empty bucket in [since_ts, until_ts), ordered oldest first."""
    con = _connect(db_path)
    try:
        cur = con.execute(
            "SELECT (ts - ts %% ?) AS bucket_ts, %s FROM readings "
            "WHERE ts >= ? AND ts < ? GROUP BY bucket_ts ORDER BY bucket_ts"
            % _AVG_LIST_SQL,
            (bucket_seconds, since_ts, until_ts),
        )
        columns = [d[0] for d in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]
    finally:
        con.close()


def query_range_preset(db_path, range_name, now=None):
    preset = RANGE_PRESETS.get(range_name)
    if preset is None:
        return None
    span_seconds, bucket_seconds = preset
    until_ts = int(now if now is not None else time.time())
    since_ts = until_ts - span_seconds
    return {
        "range": range_name,
        "bucket_seconds": bucket_seconds,
        "points": query_history(db_path, since_ts, until_ts, bucket_seconds),
    }


def coarsen_year(db_path, year):
    """Consolidates every raw row in the given calendar year (local time) down to
    COARSEN_BUCKET_SECONDS-resolution averages, in a single transaction. Safe to call
    more than once for the same year - re-coarsening already-coarsened rows is a no-op
    (averaging 5-minute buckets by 5-minute buckets yields the same rows back)."""
    year_start = int(datetime(year, 1, 1).timestamp())
    year_end = int(datetime(year + 1, 1, 1).timestamp())
    con = _connect(db_path)
    try:
        with con:
            rows = con.execute(
                "SELECT (ts - ts %% ?) AS bucket_ts, %s FROM readings "
                "WHERE ts >= ? AND ts < ? GROUP BY bucket_ts"
                % _AVG_LIST_SQL,
                (COARSEN_BUCKET_SECONDS, year_start, year_end),
            ).fetchall()
            con.execute("DELETE FROM readings WHERE ts >= ? AND ts < ?", (year_start, year_end))
            if rows:
                con.executemany(
                    "INSERT INTO readings (ts, %s) VALUES (%s)"
                    % (_FIELD_LIST_SQL, _INSERT_PLACEHOLDERS_SQL),
                    rows,
                )
            con.execute(
                "INSERT OR REPLACE INTO coarsen_log (year, coarsened_at, rows_after) "
                "VALUES (?, ?, ?)",
                (year, datetime.now().isoformat(timespec="seconds"), len(rows)),
            )
    finally:
        con.close()
    return len(rows)


def maybe_coarsen_old_years(db_path, now=None):
    """Coarsens the year that just became more than a year old, if it hasn't been
    already. E.g. on any day in 2027, 2025 is eligible (2026 is not yet - it's still
    less than a year old). Returns the year coarsened, or None if nothing was due."""
    now = now or datetime.now()
    target_year = now.year - 2
    if target_year < 1970:
        return None
    con = _connect(db_path)
    try:
        already_done = con.execute(
            "SELECT 1 FROM coarsen_log WHERE year = ?", (target_year,)
        ).fetchone()
    finally:
        con.close()
    if already_done:
        return None
    rows_after = coarsen_year(db_path, target_year)
    logger.info("Coarsened %d history to %ds resolution (%d buckets remain)",
                target_year, COARSEN_BUCKET_SECONDS, rows_after)
    return target_year


class HistoryLogger:
    """Background thread that samples the reader once a minute and does the once-a-day
    check for a newly-eligible year to coarsen."""

    def __init__(self, reader, db_path, sample_interval_seconds=60):
        self._reader = reader
        self._db_path = db_path
        self._sample_interval_seconds = (sample_interval_seconds
                                          if sample_interval_seconds > 0 else 60)
        self._stop_requested = threading.Event()
        self._thread = threading.Thread(target=self._run, name="HistoryLogger", daemon=True)
        init_db(db_path)

    def start(self):
        self._thread.start()
        logger.info("History logging to %s every %ds", self._db_path,
                    self._sample_interval_seconds)

    def stop(self):
        self._stop_requested.set()
        self._thread.join(timeout=5.0)

    def _run(self):
        last_coarsen_check = None
        while not self._stop_requested.is_set():
            try:
                snapshot = self._reader.get_snapshot()
                if snapshot.get("connected"):
                    insert_reading(self._db_path, snapshot)
            except sqlite3.Error as e:
                logger.warning("Failed to log a history sample: %s", e)

            today = date.today()
            if today != last_coarsen_check:
                last_coarsen_check = today
                try:
                    maybe_coarsen_old_years(self._db_path)
                except sqlite3.Error as e:
                    logger.warning("Failed to check/coarsen old history: %s", e)

            self._stop_requested.wait(self._sample_interval_seconds)
