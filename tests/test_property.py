"""Compare incremental aggregates against a brute-force recomputation."""

import os
import random
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, service  # noqa: E402
from app.domain import Interval, ReportContent, intersect  # noqa: E402


def _reset(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    db.initialize(path)


def _brute_force(path: str, month: str, threshold: int) -> tuple[int, int, int, set[str]]:
    conn = sqlite3.connect(path)
    samples = [
        row[0]
        for row in conn.execute(
            "SELECT sample_no FROM samples WHERE sampling_month = ?", (month,)
        )
    ]
    denominator = 0
    lower = 0
    upper = 0
    retest: set[str] = set()
    with_reports = 0
    for sample in samples:
        rows = conn.execute(
            "SELECT r.lower, r.upper, r.upper_inclusive FROM reports r "
            "JOIN (SELECT report_no, MAX(revision) AS revision FROM reports "
            "      WHERE sample_no = ? GROUP BY report_no) c "
            "  ON c.report_no = r.report_no AND c.revision = r.revision "
            "WHERE r.sample_no = ?",
            (sample, sample),
        ).fetchall()
        if not rows:
            continue
        with_reports += 1
        merged = None
        for lower_, upper_, inclusive in rows:
            interval = Interval(lower_, upper_, bool(inclusive))
            merged = interval if merged is None else intersect(merged, interval)
            if merged is None:
                break
        if merged is None:
            retest.add(sample)
            continue
        denominator += 1
        if merged.lower >= threshold:
            lower += 1
        if merged.max_integer >= threshold:
            upper += 1
    conn.close()
    return with_reports, denominator, lower, upper, retest


def test_incremental_matches_brute_force(tmp_path: Path):
    path = str(tmp_path / "tp.sqlite3")
    _reset(path)
    rng = random.Random(20261125)

    samples = [f"S{i:03d}" for i in range(25)]
    for sample in samples:
        service.create_sample(path, sample, "2026-11")
        for report_index in range(3):
            report_no = f"{sample}-R{report_index}"
            revision = 1
            for _ in range(rng.randint(1, 3)):
                if rng.random() < 0.3:
                    content = ReportContent.nondetect(rng.randint(1, 40))
                else:
                    lower = rng.randint(0, 80)
                    content = ReportContent.measured(lower, lower + rng.randint(0, 30))
                service.submit_report(path, sample, report_no, revision, content)
                revision += 1

    for threshold in range(0, 91, 7):
        doc = service.month_summary(path, "2026-11", threshold)
        total, denominator, lower, upper, retest = _brute_force(
            path, "2026-11", threshold
        )
        assert doc["total_samples"] == total
        assert doc["denominator"] == denominator
        assert doc["at_or_above_lower_bound"] == lower
        assert doc["at_or_above_upper_bound"] == upper
        assert set(doc["retest_samples"]) == retest

    # After a batch of corrections the invariants must still agree.
    for sample in samples[::3]:
        service.submit_report(
            path, sample, f"{sample}-R0", 99, ReportContent.measured(100, 120)
        )
    for threshold in range(0, 121, 11):
        doc = service.month_summary(path, "2026-11", threshold)
        total, denominator, lower, upper, retest = _brute_force(
            path, "2026-11", threshold
        )
        assert (doc["total_samples"], doc["denominator"]) == (total, denominator)
        assert doc["at_or_above_lower_bound"] == lower
        assert doc["at_or_above_upper_bound"] == upper
        assert set(doc["retest_samples"]) == retest
