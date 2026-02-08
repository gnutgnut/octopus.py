"""SQLite database layer for Octopus Energy tracker."""

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS consumption (
    interval_start TEXT PRIMARY KEY,
    interval_end   TEXT NOT NULL,
    kwh            REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS unit_rates (
    valid_from       TEXT PRIMARY KEY,
    valid_to         TEXT,
    value_exc_vat    REAL NOT NULL,
    value_inc_vat    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS standing_charges (
    valid_from       TEXT PRIMARY KEY,
    valid_to         TEXT,
    value_exc_vat    REAL NOT NULL,
    value_inc_vat    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_type     TEXT NOT NULL,
    synced_at     TEXT NOT NULL,
    period_from   TEXT,
    period_to     TEXT,
    record_count  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_consumption_start ON consumption(interval_start);
CREATE INDEX IF NOT EXISTS idx_unit_rates_from ON unit_rates(valid_from);
CREATE INDEX IF NOT EXISTS idx_standing_charges_from ON standing_charges(valid_from);
CREATE INDEX IF NOT EXISTS idx_sync_log_type ON sync_log(sync_type);
"""


class OctopusDB:
    def __init__(self, db_path: str | Path):
        self.db_path = str(db_path)
        self._conn = None

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def init_schema(self):
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()
        log.debug("Database schema initialised at %s", self.db_path)

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None

    # ── Upsert methods ──────────────────────────────────────────────

    def upsert_consumption(self, records: list[dict]) -> int:
        if not records:
            return 0
        self.conn.executemany(
            """INSERT OR REPLACE INTO consumption (interval_start, interval_end, kwh)
               VALUES (:interval_start, :interval_end, :consumption)""",
            records,
        )
        self.conn.commit()
        log.info("Upserted %d consumption records", len(records))
        return len(records)

    def upsert_unit_rates(self, records: list[dict]) -> int:
        if not records:
            return 0
        self.conn.executemany(
            """INSERT OR REPLACE INTO unit_rates
               (valid_from, valid_to, value_exc_vat, value_inc_vat)
               VALUES (:valid_from, :valid_to, :value_exc_vat, :value_inc_vat)""",
            records,
        )
        self.conn.commit()
        log.info("Upserted %d unit rate records", len(records))
        return len(records)

    def upsert_standing_charges(self, records: list[dict]) -> int:
        if not records:
            return 0
        self.conn.executemany(
            """INSERT OR REPLACE INTO standing_charges
               (valid_from, valid_to, value_exc_vat, value_inc_vat)
               VALUES (:valid_from, :valid_to, :value_exc_vat, :value_inc_vat)""",
            records,
        )
        self.conn.commit()
        log.info("Upserted %d standing charge records", len(records))
        return len(records)

    def log_sync(self, sync_type: str, period_from: str | None,
                 period_to: str | None, record_count: int):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """INSERT INTO sync_log (sync_type, synced_at, period_from, period_to, record_count)
               VALUES (?, ?, ?, ?, ?)""",
            (sync_type, now, period_from, period_to, record_count),
        )
        self.conn.commit()

    # ── Query methods ────────────────────────────────────────────────

    def last_sync(self, sync_type: str) -> dict | None:
        row = self.conn.execute(
            """SELECT * FROM sync_log WHERE sync_type = ?
               ORDER BY synced_at DESC LIMIT 1""",
            (sync_type,),
        ).fetchone()
        return dict(row) if row else None

    def get_consumption(self, period_from: str, period_to: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT interval_start, interval_end, kwh
               FROM consumption
               WHERE interval_start >= ? AND interval_start < ?
               ORDER BY interval_start""",
            (period_from, period_to),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_consumption_grouped(self, period_from: str, period_to: str,
                                group: str = "day") -> list[dict]:
        if group == "day":
            date_expr = "substr(interval_start, 1, 10)"
        elif group == "week":
            # ISO week: YYYY-Www
            date_expr = "strftime('%Y-W%W', interval_start)"
        elif group == "month":
            date_expr = "substr(interval_start, 1, 7)"
        else:
            raise ValueError(f"Unknown group: {group}")

        rows = self.conn.execute(
            f"""SELECT {date_expr} AS period,
                       SUM(kwh) AS total_kwh,
                       COUNT(*) AS readings
                FROM consumption
                WHERE interval_start >= ? AND interval_start < ?
                GROUP BY period
                ORDER BY period""",
            (period_from, period_to),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_unit_rates(self, period_from: str, period_to: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT valid_from, valid_to, value_exc_vat, value_inc_vat
               FROM unit_rates
               WHERE valid_from < ? AND (valid_to IS NULL OR valid_to > ?)
               ORDER BY valid_from""",
            (period_to, period_from),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_standing_charges(self, period_from: str, period_to: str) -> list[dict]:
        rows = self.conn.execute(
            """SELECT valid_from, valid_to, value_exc_vat, value_inc_vat
               FROM standing_charges
               WHERE valid_from < ? AND (valid_to IS NULL OR valid_to > ?)
               ORDER BY valid_from""",
            (period_to, period_from),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_cost_data(self, period_from: str, period_to: str,
                      group: str = "day") -> list[dict]:
        """Join consumption with unit rates to compute costs per period.

        Standing charges are added in Python after this query since they
        apply per-day rather than per half-hour.
        """
        if group == "day":
            date_expr = "substr(c.interval_start, 1, 10)"
        elif group == "week":
            date_expr = "strftime('%Y-W%W', c.interval_start)"
        elif group == "month":
            date_expr = "substr(c.interval_start, 1, 7)"
        else:
            raise ValueError(f"Unknown group: {group}")

        rows = self.conn.execute(
            f"""SELECT {date_expr} AS period,
                       SUM(c.kwh) AS total_kwh,
                       SUM(c.kwh * r.value_inc_vat) AS usage_cost_pence,
                       COUNT(*) AS readings
                FROM consumption c
                LEFT JOIN unit_rates r ON
                    r.valid_from <= c.interval_start
                    AND (r.valid_to IS NULL OR r.valid_to > c.interval_start)
                WHERE c.interval_start >= ? AND c.interval_start < ?
                GROUP BY period
                ORDER BY period""",
            (period_from, period_to),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_standing_charge_for_date(self, date_str: str) -> float | None:
        """Get the standing charge (inc VAT, pence/day) applicable on a date."""
        row = self.conn.execute(
            """SELECT value_inc_vat FROM standing_charges
               WHERE valid_from <= ? AND (valid_to IS NULL OR valid_to > ?)
               ORDER BY valid_from DESC LIMIT 1""",
            (date_str, date_str),
        ).fetchone()
        return row["value_inc_vat"] if row else None

    def export_all(self) -> dict:
        """Export all table data as a dict for JSON serialisation."""
        result = {}
        for table in ("consumption", "unit_rates", "standing_charges", "sync_log"):
            rows = self.conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            result[table] = [dict(r) for r in rows]
        return result
