import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import create_app  # noqa: E402
from app.domain import Interval, intersect  # noqa: E402


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "tp.sqlite3")


@pytest.fixture()
def client(db_path: str):
    with TestClient(create_app(db_path)) as test_client:
        yield test_client


def post_report(client, sample_no, report_no, revision, result, **extra):
    body = {"report_no": report_no, "revision": revision, "result": result, **extra}
    return client.post(f"/samples/{sample_no}/reports", json=body)


def summary(client, month="2026-11", threshold=20):
    response = client.get("/summary", params={"month": month, "threshold": threshold})
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Interval algebra
# ---------------------------------------------------------------------------


def test_intersect_closed_and_half_open():
    closed = Interval(10, 30, True)
    assert intersect(closed, Interval(20, 40, True)) == Interval(20, 30, True)
    # [0, 20) intersect [20, 40] is empty: 20 is not in the half-open interval.
    assert intersect(Interval(0, 20, False), Interval(20, 40, True)) is None
    # [0, 20) intersect [19, 40] leaves the single integer 19.
    assert intersect(Interval(0, 20, False), Interval(19, 40, True)) == Interval(
        19, 20, False
    )
    # A shared exclusive endpoint stays exclusive.
    assert intersect(Interval(0, 20, False), Interval(5, 20, False)) == Interval(
        5, 20, False
    )


def test_nondetect_threshold_uses_half_open_bound():
    # Detection limit 20 means [0, 20): the value 20 itself is impossible.
    interval = Interval(0, 20, False)
    assert not interval.can_reach_or_exceed(20)
    assert interval.can_reach_or_exceed(19)
    assert interval.max_integer == 19


# ---------------------------------------------------------------------------
# Report ingestion
# ---------------------------------------------------------------------------


def test_sample_registration_and_fixed_month(client):
    created = client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    assert created.status_code == 201
    again = client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    assert again.status_code == 201
    conflict = client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-12"})
    assert conflict.status_code == 409
    assert "2026-11" in conflict.json()["detail"]


def test_invalid_month_and_interval_rejected(client):
    bad_month = client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-13"})
    assert bad_month.status_code == 422
    bad_query = client.get("/summary", params={"month": "2026-13", "threshold": 20})
    assert bad_query.status_code == 422
    assert "month" in bad_query.json()["detail"]
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    inverted = post_report(
        client, "S1", "R1", 1, {"kind": "measured", "lower": 50, "upper": 20}
    )
    assert inverted.status_code == 422
    zero_limit = post_report(
        client, "S1", "R1", 1, {"kind": "nondetect", "detection_limit": 0}
    )
    assert zero_limit.status_code == 422
    negative = post_report(
        client, "S1", "R1", 1, {"kind": "measured", "lower": -1, "upper": 5}
    )
    assert negative.status_code == 422


def test_report_number_is_global_and_idempotent(client):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    client.post("/samples", json={"sample_no": "S2", "sampling_month": "2026-11"})
    first = post_report(client, "S1", "R1", 1, {"kind": "measured", "lower": 10, "upper": 20})
    assert first.status_code == 201
    assert first.json()["accepted"] is True
    assert first.json()["idempotent"] is False

    repeat = post_report(client, "S1", "R1", 1, {"kind": "measured", "lower": 10, "upper": 20})
    assert repeat.status_code == 200
    assert repeat.json()["accepted"] is False
    assert repeat.json()["idempotent"] is True
    # Re-posting must not add a second report row.
    assert len(repeat.json()["sample"]["reports"]) == 1

    clash = post_report(client, "S1", "R1", 1, {"kind": "measured", "lower": 10, "upper": 21})
    assert clash.status_code == 409

    stolen = post_report(client, "S2", "R1", 1, {"kind": "measured", "lower": 10, "upper": 20})
    assert stolen.status_code == 409


def test_only_highest_revision_counts(client):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    post_report(client, "S1", "R1", 1, {"kind": "measured", "lower": 100, "upper": 200})
    revised = post_report(
        client, "S1", "R1", 2, {"kind": "measured", "lower": 30, "upper": 40}
    )
    assert revised.status_code == 201
    assert revised.json()["sample"]["current_interval"]["lower"] == 30
    assert revised.json()["sample"]["current_interval"]["upper"] == 40

    stale = post_report(client, "S1", "R1", 1, {"kind": "measured", "lower": 30, "upper": 40})
    assert stale.status_code == 409  # old revision cannot be re-used with new content
    late = post_report(
        client, "S1", "R1", 1, {"kind": "measured", "lower": 1, "upper": 2}
    )
    assert late.status_code == 409

    summary_doc = summary(client, threshold=100)
    # Revision 2 no longer reaches 100, even though revision 1 did.
    assert summary_doc["at_or_above_upper_bound"] == 0


# ---------------------------------------------------------------------------
# Intersection, retest and denominator
# ---------------------------------------------------------------------------


def test_intersection_marks_retest_and_excludes_denominator(client):
    for sample in ("S1", "S2", "S3"):
        client.post("/samples", json={"sample_no": sample, "sampling_month": "2026-11"})

    # S1: 30..40 and 35..60 -> 35..40
    post_report(client, "S1", "S1-A", 1, {"kind": "measured", "lower": 30, "upper": 40})
    post_report(client, "S1", "S1-B", 1, {"kind": "measured", "lower": 35, "upper": 60})
    # S2: non-detect limit 20 and 25..30 -> [0,20) vs [25,30] is empty.
    post_report(client, "S2", "S2-A", 1, {"kind": "nondetect", "detection_limit": 20})
    post_report(client, "S2", "S2-B", 1, {"kind": "measured", "lower": 25, "upper": 30})
    # S3: 10..15 and 16..20 -> empty -> retest
    post_report(client, "S3", "S3-A", 1, {"kind": "measured", "lower": 10, "upper": 15})
    post_report(client, "S3", "S3-B", 1, {"kind": "measured", "lower": 16, "upper": 20})

    doc = summary(client, threshold=20)
    assert doc["total_samples"] == 3
    assert doc["denominator"] == 1  # only S1 has a non-empty intersection
    assert doc["retest_samples"] == ["S2", "S3"]
    assert doc["retest_count"] == 2
    # S1 interval 35..40 is fully >= 20, so both bounds count it.
    assert doc["at_or_above_lower_bound"] == 1
    assert doc["at_or_above_upper_bound"] == 1


def test_bound_counts_differ_when_interval_straddles_threshold(client):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    post_report(client, "S1", "A", 1, {"kind": "measured", "lower": 10, "upper": 30})
    low = summary(client, threshold=20)
    assert low["denominator"] == 1
    assert low["at_or_above_lower_bound"] == 0  # could be below 20
    assert low["at_or_above_upper_bound"] == 1  # could be at/above 20

    high = summary(client, threshold=31)
    assert high["at_or_above_lower_bound"] == 0
    assert high["at_or_above_upper_bound"] == 0

    exact = summary(client, threshold=30)
    assert exact["at_or_above_lower_bound"] == 0
    assert exact["at_or_above_upper_bound"] == 1  # closed interval includes 30


def test_nondetect_limit_is_strictly_below_limit(client):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    post_report(client, "S1", "A", 1, {"kind": "nondetect", "detection_limit": 20})
    at_limit = summary(client, threshold=20)
    assert at_limit["denominator"] == 1
    assert at_limit["at_or_above_lower_bound"] == 0
    assert at_limit["at_or_above_upper_bound"] == 0  # [0,20) never contains 20

    below = summary(client, threshold=19)
    assert below["at_or_above_upper_bound"] == 1  # 19 is in [0,20)
    assert below["at_or_above_lower_bound"] == 0  # 0 is not >= 19


# ---------------------------------------------------------------------------
# Corrections are local and atomic
# ---------------------------------------------------------------------------


def test_correction_recomputes_only_target_sample(client, db_path):
    for sample in ("S1", "S2"):
        client.post("/samples", json={"sample_no": sample, "sampling_month": "2026-11"})
    post_report(client, "S1", "S1-A", 1, {"kind": "measured", "lower": 10, "upper": 20})
    post_report(client, "S2", "S2-A", 1, {"kind": "measured", "lower": 50, "upper": 60})

    conn = sqlite3.connect(db_path)
    s2_before = conn.execute(
        "SELECT updated_at FROM sample_state WHERE sample_no = 'S2'"
    ).fetchone()
    s2_reports_before = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE sample_no = 'S2'"
    ).fetchone()
    conn.close()

    post_report(client, "S1", "S1-A", 2, {"kind": "measured", "lower": 15, "upper": 18})

    conn = sqlite3.connect(db_path)
    s2_after = conn.execute(
        "SELECT updated_at FROM sample_state WHERE sample_no = 'S2'"
    ).fetchone()
    s2_reports_after = conn.execute(
        "SELECT COUNT(*) FROM reports WHERE sample_no = 'S2'"
    ).fetchone()
    s1_state = conn.execute(
        "SELECT lower, upper FROM sample_state WHERE sample_no = 'S1'"
    ).fetchone()
    totals = conn.execute(
        "SELECT total_samples, denominator, retest_count FROM monthly_summary "
        "WHERE month = '2026-11'"
    ).fetchone()
    conn.close()

    assert s2_before == s2_after
    assert s2_reports_before == s2_reports_after
    assert s1_state == (15, 18)
    # Exactly two samples, both in the denominator, none retested.
    assert totals == (2, 2, 0)

    doc = summary(client, threshold=15)
    assert doc["at_or_above_lower_bound"] == 2  # S1 15..18 and S2 50..60
    assert doc["at_or_above_upper_bound"] == 2


def test_correction_can_resolve_retest_and_vice_versa(client):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    post_report(client, "S1", "A", 1, {"kind": "measured", "lower": 10, "upper": 15})
    post_report(client, "S1", "B", 1, {"kind": "measured", "lower": 16, "upper": 20})
    assert summary(client)["retest_samples"] == ["S1"]
    assert summary(client)["denominator"] == 0

    # Correct B to overlap A.
    post_report(client, "S1", "B", 2, {"kind": "measured", "lower": 15, "upper": 18})
    doc = summary(client)
    assert doc["retest_samples"] == []
    assert doc["retest_count"] == 0
    assert doc["denominator"] == 1

    # And back to an empty intersection, where B's new revision is the culprit.
    post_report(client, "S1", "B", 3, {"kind": "measured", "lower": 30, "upper": 40})
    doc = summary(client)
    assert doc["retest_samples"] == ["S1"]
    assert doc["denominator"] == 0
    assert doc["at_or_above_upper_bound"] == 0


def test_months_are_isolated(client):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    client.post("/samples", json={"sample_no": "S2", "sampling_month": "2026-12"})
    post_report(client, "S1", "NOV-A", 1, {"kind": "measured", "lower": 50, "upper": 60})
    post_report(client, "S2", "DEC-A", 1, {"kind": "measured", "lower": 50, "upper": 60})
    post_report(client, "S2", "DEC-B", 1, {"kind": "measured", "lower": 10, "upper": 20})

    nov = summary(client, month="2026-11")
    dec = summary(client, month="2026-12")
    assert nov["total_samples"] == 1 and nov["denominator"] == 1
    assert dec["total_samples"] == 1 and dec["denominator"] == 0
    assert dec["retest_samples"] == ["S2"]
    empty = summary(client, month="2026-10")
    assert {k: v for k, v in empty.items() if k != "calculated_at"} == {
        "month": "2026-10",
        "threshold": 20,
        "total_samples": 0,
        "denominator": 0,
        "at_or_above_lower_bound": 0,
        "at_or_above_upper_bound": 0,
        "retest_count": 0,
        "retest_samples": [],
    }


def test_retest_endpoint(client):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    client.post("/samples", json={"sample_no": "S2", "sampling_month": "2026-11"})
    post_report(client, "S1", "S1-A", 1, {"kind": "measured", "lower": 10, "upper": 10})
    post_report(client, "S1", "S1-B", 1, {"kind": "measured", "lower": 11, "upper": 12})
    post_report(client, "S2", "S2-A", 1, {"kind": "measured", "lower": 10, "upper": 12})
    response = client.get("/retest", params={"month": "2026-11"})
    assert response.status_code == 200
    assert response.json()["retest_samples"] == ["S1"]


# ---------------------------------------------------------------------------
# Failure atomicity and snapshot consistency
# ---------------------------------------------------------------------------


def test_failed_write_leaves_previous_results(client, db_path, monkeypatch):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    post_report(client, "S1", "A", 1, {"kind": "measured", "lower": 50, "upper": 60})
    before = summary(client, threshold=50)

    from app import service

    def explode(*args, **kwargs):
        raise sqlite3.OperationalError("simulated disk failure")

    # Fail after the report row is inserted but before the summary is updated.
    monkeypatch.setattr(service, "_bump_hist", explode)
    response = post_report(
        client, "S1", "A", 2, {"kind": "measured", "lower": 10, "upper": 20}
    )
    assert response.status_code == 500
    monkeypatch.undo()

    after = summary(client, threshold=50)
    assert after["denominator"] == before["denominator"]
    assert after["at_or_above_lower_bound"] == before["at_or_above_lower_bound"]
    assert after["at_or_above_upper_bound"] == before["at_or_above_upper_bound"]
    # The failed revision must not be visible either.
    sample = client.get("/samples/S1").json()
    assert [r["revision"] for r in sample["reports"]] == [1]
    assert sample["current_interval"]["lower"] == 50


def test_unknown_sample_returns_404(client):
    assert client.get("/samples/missing").status_code == 404
    orphan = post_report(
        client, "missing", "A", 1, {"kind": "measured", "lower": 1, "upper": 2}
    )
    assert orphan.status_code == 404


def test_report_can_create_sample_when_month_given(client):
    response = post_report(
        client,
        "S9",
        "A",
        1,
        {"kind": "measured", "lower": 5, "upper": 6},
        sampling_month="2026-11",
    )
    assert response.status_code == 201
    assert response.json()["sample"]["sampling_month"] == "2026-11"
    summary_doc = summary(client, threshold=5)
    assert summary_doc["denominator"] == 1


def test_inconsistent_summary_is_detected(client, db_path):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    post_report(client, "S1", "A1", 1, {"kind": "measured", "lower": 10, "upper": 20})
    post_report(client, "S1", "A2", 1, {"kind": "measured", "lower": 30, "upper": 40})
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE monthly_summary SET retest_count = 0 WHERE month = '2026-11'")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError):
        summary(client)


def test_openapi_documents_tryable_endpoints(client):
    spec = client.get("/openapi.json").json()
    assert spec["info"]["title"]
    assert "/summary" in spec["paths"]
    assert "get" in spec["paths"]["/summary"]
    params = {p["name"] for p in spec["paths"]["/summary"]["get"]["parameters"]}
    assert {"month", "threshold"} <= params
    body_schema = spec["paths"]["/samples/{sample_no}/reports"]["post"]["requestBody"]
    assert body_schema["content"]["application/json"]["schema"]
    assert client.get("/docs").status_code == 200

    # Swagger "Try it out" prefills these; keep them present.
    schemas = spec["components"]["schemas"]
    assert schemas["SampleCreate"]["examples"]
    assert len(schemas["ReportSubmit"]["examples"]) == 2
    kinds = {ex["result"]["kind"] for ex in schemas["ReportSubmit"]["examples"]}
    assert kinds == {"measured", "nondetect"}
    month_param = next(
        p for p in spec["paths"]["/summary"]["get"]["parameters"] if p["name"] == "month"
    )
    assert month_param["schema"].get("examples") == ["2026-11"]


def test_openapi_paths_are_complete(client):
    spec = client.get("/openapi.json").json()
    assert set(spec["paths"]) == {
        "/healthz",
        "/samples",
        "/samples/{sample_no}",
        "/samples/{sample_no}/reports",
        "/summary",
        "/retest",
    }
