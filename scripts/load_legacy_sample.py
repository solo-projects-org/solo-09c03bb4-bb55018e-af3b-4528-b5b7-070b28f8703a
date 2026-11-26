#!/usr/bin/env python3
"""Load the legacy-backup sample into a tp-check database.

The sample ships complete lab-report originals (with each report's highest
revision) plus stale derived tables, exactly what a restored backup looks like
when originals were back-filled but the monthly summary was not. After loading,
open Swagger and call ``POST /months/2026-10/rebuild`` then
``GET /summary?month=2026-10&threshold=20``.

Usage:
    python3 scripts/load_legacy_sample.py samples/legacy_2026-10.json --db /data/tp.sqlite3
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.domain import ReportContent  # noqa: E402
from app.service import submit_report  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def load(sample: dict, path: str) -> None:
    month = sample["month"]
    db.initialize(path)

    for entry in sample["samples"]:
        sample_no = entry["sample_no"]
        for report in entry["reports"]:
            result = report["result"]
            content = (
                ReportContent.measured(result["lower"], result["upper"])
                if result["kind"] == "measured"
                else ReportContent.nondetect(result["detection_limit"])
            )
            submit_report(
                path,
                sample_no=sample_no,
                report_no=report["report_no"],
                revision=report["revision"],
                content=content,
                sampling_month=month,
            )

    stale = sample["stale_derived"]
    with db.write_transaction(path) as conn:
        conn.execute(
            "DELETE FROM sample_state WHERE sample_no IN "
            "(SELECT sample_no FROM samples WHERE sampling_month = ?)",
            (month,),
        )
        conn.execute("DELETE FROM monthly_lower_hist WHERE month = ?", (month,))
        conn.execute("DELETE FROM monthly_max_hist WHERE month = ?", (month,))
        conn.execute("DELETE FROM monthly_summary WHERE month = ?", (month,))

        for state in stale["state"]:
            if state["status"] == "retest":
                conn.execute(
                    "INSERT INTO sample_state(sample_no, status, lower, upper, "
                    "upper_inclusive, updated_at) "
                    "VALUES (?, 'retest', NULL, NULL, NULL, ?)",
                    (state["sample_no"], _now()),
                )
            else:
                conn.execute(
                    "INSERT INTO sample_state(sample_no, status, lower, upper, "
                    "upper_inclusive, updated_at) VALUES (?, 'ok', ?, ?, ?, ?)",
                    (
                        state["sample_no"],
                        state["lower"],
                        state["upper"],
                        1 if state["upper_inclusive"] else 0,
                        _now(),
                    ),
                )
        for value, count in stale["lower_hist"]:
            conn.execute(
                "INSERT INTO monthly_lower_hist(month, value, sample_count) "
                "VALUES (?, ?, ?)",
                (month, value, count),
            )
        for value, count in stale["max_hist"]:
            conn.execute(
                "INSERT INTO monthly_max_hist(month, value, sample_count) "
                "VALUES (?, ?, ?)",
                (month, value, count),
            )
        conn.execute(
            "INSERT INTO monthly_summary(month, total_samples, denominator, "
            "retest_count, updated_at) VALUES (?, ?, ?, ?, ?)",
            (
                month,
                len(stale["state"]),
                stale["denominator"],
                stale["retest_count"],
                _now(),
            ),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", type=Path, help="Path to legacy sample JSON")
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite path (default: $TP_DB_PATH or data/tp.sqlite3)",
    )
    args = parser.parse_args()
    sample = json.loads(args.sample.read_text(encoding="utf-8"))
    path = db.database_path(args.db)
    load(sample, path)
    print(f"loaded legacy sample month={sample['month']} into {path}")


if __name__ == "__main__":
    main()
