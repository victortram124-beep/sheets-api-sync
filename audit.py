"""
Audit log — SQLite-backed change ledger for the sync engine.

Every read, write, and conflict resolution is appended here so a client can
answer "who changed what, when, and which side did it come from?" months
after the fact. Critical for any sync touching billing, CRM, or HR data.
"""

from __future__ import annotations

import csv
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("audit")

# Bumped whenever the schema changes; lets us add migrations later without
# silently breaking older databases.
_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    action      TEXT    NOT NULL,  -- read | write | conflict | skip | error
    source      TEXT    NOT NULL,  -- sheet | api | sync
    row_id      TEXT,
    field       TEXT,
    old_value   TEXT,
    new_value   TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_row ON audit_log(row_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts  ON audit_log(ts);
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class AuditLog:
    """Thin wrapper over a SQLite file. Safe to instantiate many times."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        # check_same_thread=False so the CLI and a daemon thread can share.
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as c:
            c.executescript(_SCHEMA)
            c.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
                ("version", str(_SCHEMA_VERSION)),
            )

    def record(
        self,
        action: str,
        source: str,
        *,
        row_id: Optional[str] = None,
        field: Optional[str] = None,
        old_value: Any = None,
        new_value: Any = None,
        note: Optional[str] = None,
    ) -> None:
        """Append a row. Values are str()'d so callers don't have to."""
        with self._conn() as c:
            c.execute(
                """INSERT INTO audit_log
                   (ts, action, source, row_id, field, old_value, new_value, note)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    action,
                    source,
                    row_id,
                    field,
                    None if old_value is None else str(old_value),
                    None if new_value is None else str(new_value),
                    note,
                ),
            )

    def dump_csv(self, output_path: str | Path) -> int:
        """Export the full log to CSV. Returns row count."""
        with self._conn() as c:
            cur = c.execute(
                "SELECT ts, action, source, row_id, field, old_value, new_value, note "
                "FROM audit_log ORDER BY id ASC"
            )
            rows = cur.fetchall()
        out = Path(output_path)
        with out.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["ts", "action", "source", "row_id", "field", "old_value", "new_value", "note"])
            w.writerows(rows)
        log.info("Wrote %d audit rows to %s", len(rows), out)
        return len(rows)
