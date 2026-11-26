"""The bundled legacy-backup sample loads and rebuilds to documented results."""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, service  # noqa: E402
from scripts.load_legacy_sample import load  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "samples" / "legacy_2026-10.json"


def test_legacy_sample_rebuilds_to_documented_results(tmp_path: Path):
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    path = str(tmp_path / "tp.sqlite3")
    load(sample, path)

    # Before the rebuild the stale backup values are what readers see.
    stale = sample["stale_derived"]
    before = service.month_summary(path, "2026-10", 20)
    assert before["total_samples"] == len(stale["state"])
    assert before["denominator"] == stale["denominator"]
    assert before["retest_count"] == stale["retest_count"]
    assert before["retest_samples"] == ["S-2026-10-02", "S-2026-10-05"]

    built = service.rebuild_month(path, "2026-10")
    expected = sample["expected_after_rebuild"]
    assert built["total_samples"] == expected["total_samples"]
    assert built["denominator"] == expected["denominator"]
    assert built["retest_count"] == expected["retest_count"]
    assert built["retest_samples"] == expected["retest_samples"]

    after = service.month_summary(path, "2026-10", 20)
    assert after["retest_samples"] == ["S-2026-10-02"]
    assert (
        after["at_or_above_lower_bound"]
        == expected["threshold_20"]["at_or_above_lower_bound"]
    )
    assert (
        after["at_or_above_upper_bound"]
        == expected["threshold_20"]["at_or_above_upper_bound"]
    )

    # Highest revision is used: the superseded 99..100 no longer counts.
    conn = sqlite3.connect(path)
    state = conn.execute(
        "SELECT status, lower, upper FROM sample_state WHERE sample_no = 'S-2026-10-01'"
    ).fetchone()
    conn.close()
    assert state == ("ok", 35, 45)

    # Loading the sample twice is idempotent for report originals.
    load(sample, path)
    assert service.rebuild_month(path, "2026-10")["total_samples"] == expected[
        "total_samples"
    ]
