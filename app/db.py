"""SQLite persistence helpers.

The service uses one short-lived connection per operation. Writes use
``BEGIN IMMEDIATE`` so report revision, per-sample current interval, monthly
summary counters and histogram bins change atomically. Reads that must agree
with each other (summary counts + retest list) share one deferred transaction,
which pins a single WAL snapshot even if a writer commits in between.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    sample_no      TEXT PRIMARY KEY,
    sampling_month TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    report_no       TEXT NOT NULL,
    sample_no       TEXT NOT NULL REFERENCES samples(sample_no),
    revision        INTEGER NOT NULL CHECK (revision >= 1),
    kind            TEXT NOT NULL CHECK (kind IN ('measured', 'nondetect')),
    lower           INTEGER NOT NULL CHECK (lower >= 0),
    upper           INTEGER NOT NULL CHECK (upper >= lower),
    upper_inclusive INTEGER NOT NULL CHECK (upper_inclusive IN (0, 1)),
    detection_limit INTEGER,
    fingerprint     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (report_no, revision),
    CHECK (
        (kind = 'measured' AND upper_inclusive = 1 AND detection_limit IS NULL)
        OR
        (kind = 'nondetect' AND lower = 0 AND upper_inclusive = 0
         AND detection_limit = upper AND detection_limit > 0)
    )
);
CREATE INDEX IF NOT EXISTS idx_reports_sample ON reports(sample_no);

CREATE TABLE IF NOT EXISTS sample_state (
    sample_no       TEXT PRIMARY KEY REFERENCES samples(sample_no),
    status          TEXT NOT NULL CHECK (status IN ('ok', 'retest')),
    lower           INTEGER,
    upper           INTEGER,
    upper_inclusive INTEGER,
    updated_at      TEXT NOT NULL,
    CHECK (
        (status = 'retest' AND lower IS NULL AND upper IS NULL
         AND upper_inclusive IS NULL)
        OR
        (status = 'ok' AND lower >= 0 AND upper >= lower
         AND upper_inclusive IN (0, 1))
    )
);
CREATE INDEX IF NOT EXISTS idx_state_status ON sample_state(status);
CREATE INDEX IF NOT EXISTS idx_samples_month ON samples(sampling_month);

CREATE TABLE IF NOT EXISTS monthly_summary (
    month         TEXT PRIMARY KEY,
    total_samples INTEGER NOT NULL DEFAULT 0 CHECK (total_samples >= 0),
    denominator   INTEGER NOT NULL DEFAULT 0 CHECK (denominator >= 0),
    retest_count  INTEGER NOT NULL DEFAULT 0 CHECK (retest_count >= 0),
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monthly_lower_hist (
    month        TEXT NOT NULL,
    value        INTEGER NOT NULL CHECK (value >= 0),
    sample_count INTEGER NOT NULL CHECK (sample_count > 0),
    PRIMARY KEY (month, value)
);

CREATE TABLE IF NOT EXISTS monthly_max_hist (
    month        TEXT NOT NULL,
    value        INTEGER NOT NULL CHECK (value >= 0),
    sample_count INTEGER NOT NULL CHECK (sample_count > 0),
    PRIMARY KEY (month, value)
);
"""


def database_path(explicit: str | None = None) -> str:
    return explicit or os.environ.get("TP_DB_PATH", "data/tp.sqlite3")


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def connect(path: str) -> sqlite3.Connection:
    return _connect(path)


def initialize(path: str) -> None:
    """Create the database file, schema and persistent WAL settings."""
    if path != ":memory:":
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
    conn = _connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_transaction(path: str) -> Iterator[sqlite3.Connection]:
    """Serialized write transaction; any exception rolls every change back."""
    conn = _connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()


@contextmanager
def read_transaction(path: str) -> Iterator[sqlite3.Connection]:
    """Read transaction pinning one committed snapshot for all queries."""
    conn = _connect(path)
    try:
        conn.execute("BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")
    finally:
        conn.close()
