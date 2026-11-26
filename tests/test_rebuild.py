"""Rebuilding a sampling month from the highest revision of every report."""

import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, service  # noqa: E402
from app.api import create_app  # noqa: E402
from app.domain import ReportContent  # noqa: E402


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "tp.sqlite3")


@pytest.fixture()
def client(db_path: str):
    with TestClient(create_app(db_path)) as test_client:
        yield test_client


def post_report(client, sample_no, report_no, revision, result, **extra):
    return client.post(
        f"/samples/{sample_no}/reports",
        json={"report_no": report_no, "revision": revision, "result": result, **extra},
    )


def summary(client, month="2026-11", threshold=20):
    response = client.get("/summary", params={"month": month, "threshold": threshold})
    assert response.status_code == 200, response.text
    return response.json()


def rebuild(client, month="2026-11"):
    return client.post(f"/months/{month}/rebuild")


def _seed_consistent_month(client, month="2026-11"):
    """S1 ok, S2 retest, S3 ok — all through the incremental path."""
    for sample in ("S1", "S2", "S3"):
        client.post("/samples", json={"sample_no": sample, "sampling_month": month})
    post_report(client, "S1", "S1-A", 1, {"kind": "measured", "lower": 30, "upper": 40})
    post_report(client, "S1", "S1-B", 1, {"kind": "measured", "lower": 35, "upper": 60})
    post_report(client, "S2", "S2-A", 1, {"kind": "nondetect", "detection_limit": 20})
    post_report(client, "S2", "S2-B", 1, {"kind": "measured", "lower": 25, "upper": 30})
    post_report(client, "S3", "S3-A", 1, {"kind": "measured", "lower": 50, "upper": 70})


def test_rebuild_endpoint_runs_and_reports_totals(client):
    _seed_consistent_month(client)
    response = rebuild(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["month"] == "2026-11"
    assert body["total_samples"] == 3
    assert body["denominator"] == 2
    assert body["retest_samples"] == ["S2"]
    assert body["attempts"] >= 1


def test_rebuild_replaces_stale_aggregates_with_highest_revisions(client, db_path):
    _seed_consistent_month(client)

    # Simulate a restored backup whose derived tables drifted from report
    # originals: newest revisions exist in `reports`, but `sample_state` and
    # the monthly summary still reflect an older denominator/name list.
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE sample_state SET status = 'ok', lower = 45, upper = 45, "
        "upper_inclusive = 1 WHERE sample_no = 'S2'"
    )
    conn.execute(
        "UPDATE monthly_summary SET total_samples = 3, denominator = 3, "
        "retest_count = 0 WHERE month = '2026-11'"
    )
    conn.execute("DELETE FROM monthly_lower_hist WHERE month = '2026-11'")
    conn.execute(
        "INSERT INTO monthly_lower_hist(month, value, sample_count) "
        "VALUES ('2026-11', 45, 1)"
    )
    conn.commit()
    conn.close()

    response = rebuild(client)
    assert response.status_code == 200, response.text

    doc = summary(client, threshold=20)
    assert doc["total_samples"] == 3
    assert doc["denominator"] == 2
    assert doc["retest_count"] == 1
    assert doc["retest_samples"] == ["S2"]
    # S1 -> 35..40, S3 -> 50..70; both reach 20.
    assert doc["at_or_above_lower_bound"] == 2
    assert doc["at_or_above_upper_bound"] == 2
    retest = client.get("/retest", params={"month": "2026-11"}).json()
    assert retest["retest_samples"] == ["S2"]


def test_rebuild_uses_only_the_highest_revision(client):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    post_report(client, "S1", "R1", 1, {"kind": "measured", "lower": 100, "upper": 200})
    post_report(client, "S1", "R1", 2, {"kind": "measured", "lower": 30, "upper": 40})
    # Stored aggregates are already correct; the rebuild must not resurrect
    # revision 1's 100..200.
    rebuild(client)
    doc = summary(client, threshold=100)
    assert doc["at_or_above_upper_bound"] == 0
    assert summary(client, threshold=30)["at_or_above_lower_bound"] == 1


def test_rebuild_preserves_other_months(client):
    client.post("/samples", json={"sample_no": "NOV", "sampling_month": "2026-11"})
    client.post("/samples", json={"sample_no": "DEC", "sampling_month": "2026-12"})
    post_report(client, "NOV", "N-A", 1, {"kind": "measured", "lower": 50, "upper": 60})
    post_report(client, "DEC", "D-A", 1, {"kind": "measured", "lower": 10, "upper": 20})
    post_report(client, "DEC", "D-B", 1, {"kind": "measured", "lower": 30, "upper": 40})

    before_dec = summary(client, month="2026-12")
    rebuild(client, month="2026-11")
    after_dec = summary(client, month="2026-12")
    assert {k: v for k, v in before_dec.items() if k != "calculated_at"} == {
        k: v for k, v in after_dec.items() if k != "calculated_at"
    }
    assert after_dec["retest_samples"] == ["DEC"]


def test_rebuild_of_empty_month_is_a_clean_zero(client):
    response = rebuild(client, month="2026-09")
    assert response.status_code == 200
    assert response.json()["total_samples"] == 0
    assert response.json()["retest_samples"] == []
    doc = summary(client, month="2026-09")
    assert doc["total_samples"] == 0 and doc["denominator"] == 0


def test_rebuild_rejects_invalid_month(client):
    assert rebuild(client, month="2026-13").status_code == 422


def test_failed_rebuild_keeps_current_results(client, db_path, monkeypatch):
    _seed_consistent_month(client)
    before = summary(client, threshold=20)

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("simulated disk failure")

    monkeypatch.setattr(service, "_replace_month_aggregates", explode)
    response = rebuild(client)
    assert response.status_code == 500
    monkeypatch.undo()

    after = summary(client, threshold=20)
    assert {k: v for k, v in before.items() if k != "calculated_at"} == {
        k: v for k, v in after.items() if k != "calculated_at"
    }


def test_revision_during_rebuild_is_retried_and_included(client, monkeypatch):
    """A same-month revision committed mid-recompute must be included."""
    _seed_consistent_month(client)
    original = service._collect_rebuild_plan
    calls = {"n": 0}

    def racing_collect(conn, month):
        plan = original(conn, month)
        calls["n"] += 1
        if calls["n"] == 1:
            # Commit a new revision for this month after the plan was read but
            # before it is swapped in.
            post_report(
                client,
                "S3",
                "S3-A",
                2,
                {"kind": "measured", "lower": 5, "upper": 6},
            )
        return plan

    monkeypatch.setattr(service, "_collect_rebuild_plan", racing_collect)
    response = rebuild(client)
    monkeypatch.undo()

    assert response.status_code == 200, response.text
    assert response.json()["attempts"] == 2
    doc = summary(client, threshold=20)
    # S3 now sits at 5..6: it no longer reaches 20, so only S1 (35..40) does.
    assert doc["denominator"] == 2
    assert doc["at_or_above_upper_bound"] == 1


def test_persistent_same_month_churn_returns_409_and_keeps_results(
    client, monkeypatch
):
    _seed_consistent_month(client)
    before = summary(client, threshold=20)
    original = service._collect_rebuild_plan
    counter = {"n": 0}

    def always_racing(conn, month):
        plan = original(conn, month)
        counter["n"] += 1
        post_report(
            client,
            "S1",
            "S1-A",
            1 + counter["n"],
            {"kind": "measured", "lower": 10, "upper": 20},
        )
        return plan

    monkeypatch.setattr(service, "_collect_rebuild_plan", always_racing)
    response = rebuild(client)
    monkeypatch.undo()

    assert response.status_code == 409
    assert "retry" in response.json()["detail"].lower()
    # Nothing from the aborted rebuild was swapped in: the month still reflects
    # exactly the revisions that the incremental path committed (S1's racing
    # revision is 10..20, which makes it a second retest sample).
    after = summary(client, threshold=20)
    assert after["total_samples"] == before["total_samples"]
    assert after["retest_samples"] == ["S1", "S2"]
    assert after["retest_count"] == 2


def test_other_month_submission_does_not_invalidate_rebuild(client, monkeypatch):
    _seed_consistent_month(client)
    client.post("/samples", json={"sample_no": "DEC", "sampling_month": "2026-12"})
    original = service._collect_rebuild_plan
    state = {"done": False}

    def racing_other_month(conn, month):
        plan = original(conn, month)
        if not state["done"]:
            state["done"] = True
            post_report(
                client, "DEC", "D-A", 1, {"kind": "measured", "lower": 1, "upper": 2}
            )
        return plan

    monkeypatch.setattr(service, "_collect_rebuild_plan", racing_other_month)
    response = rebuild(client, month="2026-11")
    monkeypatch.undo()

    assert response.status_code == 200, response.text
    assert response.json()["attempts"] == 1
    nov = summary(client, month="2026-11", threshold=20)
    assert nov["retest_samples"] == ["S2"]
    dec = summary(client, month="2026-12", threshold=1)
    assert dec["denominator"] == 1


def test_readers_see_committed_results_while_rebuilding(db_path, monkeypatch):
    path = db_path
    db.initialize(path)
    for sample in ("S1", "S2"):
        service.create_sample(path, sample, "2026-11")
    service.submit_report(path, "S1", "S1-A", 1, ReportContent.measured(10, 20))
    service.submit_report(path, "S2", "S2-A", 1, ReportContent.measured(30, 40))
    before = service.month_summary(path, "2026-11", 20)

    # Corrupt the derived tables so the rebuild has work to do, then pause
    # inside the recomputation while a reader queries the month.
    with db.write_transaction(path) as conn:
        conn.execute(
            "UPDATE sample_state SET status = 'retest', lower = NULL, "
            "upper = NULL, upper_inclusive = NULL WHERE sample_no = 'S2'"
        )
        conn.execute(
            "UPDATE monthly_summary SET denominator = 1, retest_count = 1 "
            "WHERE month = '2026-11'"
        )
        conn.execute("DELETE FROM monthly_lower_hist WHERE month = '2026-11' AND value = 30")
        conn.execute("DELETE FROM monthly_max_hist WHERE month = '2026-11' AND value = 40")
    stale = service.month_summary(path, "2026-11", 20)
    assert stale["total_samples"] == 2
    assert stale["denominator"] == 1
    assert stale["retest_samples"] == ["S2"]

    observed: list[dict] = []
    original = service._collect_rebuild_plan

    def slow_collect(conn, month):
        plan = original(conn, month)
        observed.append(service.month_summary(path, month, 20))
        return plan

    monkeypatch.setattr(service, "_collect_rebuild_plan", slow_collect)
    service.rebuild_month(path, "2026-11")
    monkeypatch.undo()

    # The reader during the recomputation saw the previous committed state,
    # not a half-applied one.
    assert observed and observed[0]["total_samples"] == 2
    assert observed[0]["retest_samples"] == ["S2"]
    after = service.month_summary(path, "2026-11", 20)
    assert after["total_samples"] == 2
    assert after["denominator"] == 2
    assert {k: v for k, v in before.items() if k != "calculated_at"} == {
        k: v for k, v in after.items() if k != "calculated_at"
    }


def test_concurrent_submissions_and_rebuilds_stay_consistent(tmp_path):
    path = str(tmp_path / "tp.sqlite3")
    db.initialize(path)
    samples = [f"S{i}" for i in range(8)]
    for sample in samples:
        service.create_sample(path, sample, "2026-11")
        service.submit_report(
            path, sample, f"{sample}-A", 1, ReportContent.measured(10, 20)
        )

    stop = threading.Event()
    errors: list[BaseException] = []

    def submitter(index: int) -> None:
        sample = samples[index % len(samples)]
        revision = 2
        try:
            while not stop.is_set():
                service.submit_report(
                    path,
                    sample,
                    f"{sample}-A",
                    revision,
                    ReportContent.measured(10 + (revision % 5), 20 + (revision % 7)),
                )
                revision += 1
                time.sleep(0.001)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=submitter, args=(0,))
    thread.start()
    try:
        for _ in range(6):
            service.rebuild_month(path, "2026-11")
            doc = service.month_summary(path, "2026-11", 20)
            assert doc["denominator"] + doc["retest_count"] == doc["total_samples"]
    finally:
        stop.set()
        thread.join()

    assert not errors, errors
    # Final rebuild must agree with a clean, submission-free recomputation.
    rebuilt = service.rebuild_month(path, "2026-11")
    doc = service.month_summary(path, "2026-11", 20)
    assert doc["total_samples"] == len(samples)
    assert doc["denominator"] == len(samples)
    assert doc["retest_count"] == 0
    assert rebuilt["total_samples"] == len(samples)


def test_rebuild_interleaved_with_other_month_submissions(tmp_path):
    """Other months can keep reporting while this month is rebuilt."""
    path = str(tmp_path / "tp.sqlite3")
    db.initialize(path)
    for sample in ("N1", "N2", "N3"):
        service.create_sample(path, sample, "2026-11")
        service.submit_report(
            path, sample, f"{sample}-A", 1, ReportContent.measured(20, 30)
        )
    service.create_sample(path, "D1", "2026-12")
    service.submit_report(path, "D1", "D1-A", 1, ReportContent.measured(1, 2))
    dec_before = service.month_summary(path, "2026-12", 1)

    stop = threading.Event()
    errors: list[BaseException] = []

    def december_submitter() -> None:
        revision = 2
        try:
            while not stop.is_set():
                service.submit_report(
                    path,
                    "D1",
                    "D1-A",
                    revision,
                    ReportContent.measured(revision, revision + 5),
                )
                revision += 1
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=december_submitter)
    thread.start()
    try:
        for _ in range(12):
            built = service.rebuild_month(path, "2026-11")
            # December traffic must not cost a retry of the November rebuild.
            assert built["attempts"] == 1
            assert built["total_samples"] == 3
    finally:
        stop.set()
        thread.join()

    assert not errors, errors
    # December's own incremental aggregates remain live and consistent.
    dec = service.month_summary(path, "2026-12", 1)
    assert dec["total_samples"] == dec_before["total_samples"]
    assert dec["denominator"] == 1
    assert dec["retest_count"] == 0
    assert service.rebuild_month(path, "2026-12")["total_samples"] == 1


def test_rebuild_matches_brute_force_after_corrections(client, db_path):
    _seed_consistent_month(client)
    post_report(client, "S1", "S1-B", 2, {"kind": "measured", "lower": 5, "upper": 6})
    post_report(client, "S3", "S3-A", 2, {"kind": "measured", "lower": 80, "upper": 90})
    rebuild(client)

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT st.sample_no, st.status, st.lower, st.upper "
        "FROM sample_state AS st JOIN samples AS s "
        "  ON s.sample_no = st.sample_no WHERE s.sampling_month = '2026-11' "
        "ORDER BY st.sample_no"
    ).fetchall()
    conn.close()
    # S1 30..40 & 35..60 & 5..6 -> empty -> retest
    # S2 20-limited non-detect vs 25..30 -> empty -> retest
    # S3 80..90 -> ok
    assert rows == [
        ("S1", "retest", None, None),
        ("S2", "retest", None, None),
        ("S3", "ok", 80, 90),
    ]
