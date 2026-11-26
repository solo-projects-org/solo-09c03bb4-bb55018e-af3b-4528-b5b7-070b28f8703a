import os
import sqlite3
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, service  # noqa: E402
from app.domain import ReportContent  # noqa: E402


def _reset(path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)
    db.initialize(path)


def test_concurrent_writers_keep_aggregates_consistent(tmp_path: Path):
    path = str(tmp_path / "tp.sqlite3")
    _reset(path)

    samples = [f"S{i:02d}" for i in range(12)]
    for sample in samples:
        service.create_sample(path, sample, "2026-11")

    errors: list[BaseException] = []

    def writer(index: int) -> None:
        sample = samples[index]
        try:
            for revision in range(1, 6):
                service.submit_report(
                    path,
                    sample_no=sample,
                    report_no=f"{sample}-R1",
                    revision=revision,
                    content=ReportContent.measured(100 + index, 200 + index),
                )
                service.submit_report(
                    path,
                    sample_no=sample,
                    report_no=f"{sample}-R2",
                    revision=revision,
                    content=ReportContent.measured(150, 300),
                )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(len(samples))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors

    conn = sqlite3.connect(path)
    summary = conn.execute(
        "SELECT total_samples, denominator, retest_count FROM monthly_summary "
        "WHERE month = '2026-11'"
    ).fetchone()
    lower_hist_total = conn.execute(
        "SELECT SUM(sample_count) FROM monthly_lower_hist WHERE month = '2026-11'"
    ).fetchone()[0]
    upper_hist_total = conn.execute(
        "SELECT SUM(sample_count) FROM monthly_max_hist WHERE month = '2026-11'"
    ).fetchone()[0]
    states = conn.execute(
        "SELECT COUNT(*) FROM sample_state WHERE status = 'ok'"
    ).fetchone()[0]
    conn.close()

    assert summary == (len(samples), len(samples), 0)
    assert lower_hist_total == len(samples)
    assert upper_hist_total == len(samples)
    assert states == len(samples)


def test_read_snapshot_never_mixes_generations(tmp_path: Path):
    """A summary read must never see counts and retest list from different commits."""
    path = str(tmp_path / "tp.sqlite3")
    _reset(path)
    service.create_sample(path, "S1", "2026-11")
    service.submit_report(
        path, "S1", "R1", 1, ReportContent.measured(10, 20)
    )

    stop = threading.Event()
    observed: list[tuple[int, list[str]]] = []
    read_errors: list[BaseException] = []

    def reader() -> None:
        try:
            while not stop.is_set():
                doc = service.month_summary(path, "2026-11", 15)
                observed.append((doc["retest_count"], doc["retest_samples"]))
                assert doc["retest_count"] == len(doc["retest_samples"])
                # An "ok" sample has a non-empty current interval; a retest sample
                # must be listed. The two summaries must agree with the state.
                assert (doc["retest_count"] == 0) == (doc["denominator"] == 1)
        except BaseException as exc:  # pragma: no cover
            read_errors.append(exc)

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        # Flip S1 between a consistent state and a retest state repeatedly.
        for revision in range(2, 12):
            content = (
                ReportContent.measured(10, 20)
                if revision % 2 == 0
                else ReportContent.measured(30, 40)
            )
            service.submit_report(path, "S1", "R2", revision, content)
            service.submit_report(
                path,
                "S1",
                "R3",
                revision,
                ReportContent.measured(1, 2),
            )
    finally:
        stop.set()
        thread.join()

    assert not read_errors, read_errors
    assert observed
