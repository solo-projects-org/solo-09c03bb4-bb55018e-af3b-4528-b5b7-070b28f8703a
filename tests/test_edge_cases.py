import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api import create_app  # noqa: E402
from app.domain import ReportContent, DomainError, Interval, intersect  # noqa: E402


@pytest.fixture()
def client(tmp_path: Path):
    with TestClient(create_app(str(tmp_path / "tp.sqlite3"))) as test_client:
        yield test_client


def post(client, sample, report, revision, result, **extra):
    return client.post(
        f"/samples/{sample}/reports",
        json={"report_no": report, "revision": revision, "result": result, **extra},
    )


def test_report_number_cannot_move_between_samples(client):
    for sample in ("S1", "S2"):
        client.post("/samples", json={"sample_no": sample, "sampling_month": "2026-11"})
    assert post(client, "S1", "SHARED", 1, {"kind": "measured", "lower": 1, "upper": 2}).status_code == 201
    moved = post(client, "S2", "SHARED", 2, {"kind": "measured", "lower": 3, "upper": 4})
    assert moved.status_code == 409


def test_measured_zero_width_interval(client):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    response = post(client, "S1", "R1", 1, {"kind": "measured", "lower": 25, "upper": 25})
    assert response.status_code == 201
    doc = client.get("/summary", params={"month": "2026-11", "threshold": 25}).json()
    assert doc["denominator"] == 1
    assert doc["at_or_above_lower_bound"] == 1
    assert doc["at_or_above_upper_bound"] == 1


def test_rejected_report_does_not_disturb_aggregates(client):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    post(client, "S1", "R1", 1, {"kind": "measured", "lower": 10, "upper": 20})
    before = client.get("/summary", params={"month": "2026-11", "threshold": 10}).json()
    conflict = post(client, "S1", "R1", 1, {"kind": "measured", "lower": 10, "upper": 21})
    assert conflict.status_code == 409
    after = client.get("/summary", params={"month": "2026-11", "threshold": 10}).json()
    assert {k: v for k, v in before.items() if k != "calculated_at"} == {
        k: v for k, v in after.items() if k != "calculated_at"
    }


def test_missing_required_fields_and_types(client):
    assert client.post("/samples", json={"sample_no": "S1"}).status_code == 422
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    # Float endpoints are rejected: concentrations are integers.
    assert (
        post(client, "S1", "R1", 1, {"kind": "measured", "lower": 10.5, "upper": 20}).status_code
        == 422
    )
    assert post(client, "S1", "R1", 0, {"kind": "measured", "lower": 1, "upper": 2}).status_code == 422
    assert post(client, "S1", "R1", 1, {"kind": "nondetect"}).status_code == 422
    # Unknown discriminator kind.
    assert post(client, "S1", "R1", 1, {"kind": "below", "detection_limit": 3}).status_code == 422


def test_large_integer_endpoints(client):
    big = 2**31 - 1
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    assert post(client, "S1", "R1", 1, {"kind": "measured", "lower": big - 5, "upper": big}).status_code == 201
    doc = client.get("/summary", params={"month": "2026-11", "threshold": big}).json()
    assert doc["at_or_above_upper_bound"] == 1
    assert doc["at_or_above_lower_bound"] == 0


def test_beyond_sqlite_integer_range_rejected(client):
    client.post("/samples", json={"sample_no": "S1", "sampling_month": "2026-11"})
    too_big = 2**63
    response = post(
        client, "S1", "R1", 1, {"kind": "measured", "lower": 0, "upper": too_big}
    )
    assert response.status_code == 422


def test_domain_rejects_bad_half_open_interval():
    with pytest.raises(DomainError):
        Interval(0, 0, False)  # [0, 0) holds no integer
    with pytest.raises(DomainError):
        ReportContent.nondetect(0)
    with pytest.raises(DomainError):
        ReportContent.nondetect(True)  # bool is not accepted as an endpoint
    assert intersect(Interval(0, 5, False), Interval(5, 9, True)) is None


def test_correction_recomputes_exactly_one_sample(tmp_path, monkeypatch):
    from app import service

    path = str(tmp_path / "tp.sqlite3")
    db_initialize(path)
    for sample in ("S1", "S2", "S3"):
        service.create_sample(path, sample, "2026-11")
        service.submit_report(
            path, sample, f"{sample}-R", 1, ReportContent.measured(10, 20)
        )

    calls: list[str] = []
    original = service._current_intervals

    def recorder(conn, sample_no):
        calls.append(sample_no)
        return original(conn, sample_no)

    monkeypatch.setattr(service, "_current_intervals", recorder)
    service.submit_report(path, "S2", "S2-R", 2, ReportContent.measured(12, 18))

    # Only the corrected sample was recomputed; S1 and S3 were never scanned.
    assert calls == ["S2"]


def db_initialize(path: str) -> None:
    from app import db

    db.initialize(path)
