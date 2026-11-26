"""Application services: report ingestion, sample state and monthly summaries.

Monthly aggregates are maintained incrementally. Each water sample contributes
at most two histogram entries to its month: its current interval's lower bound
and the largest integer it can still hold. A correction touches only the
corrected sample: its previous contribution is removed and its new contribution
is added in the same transaction. No other sample is scanned.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Optional

from . import db
from .domain import (
    ConflictError,
    DomainError,
    Interval,
    NotFoundError,
    ReportContent,
    intersect,
    validate_month,
    validate_threshold,
)

LOWER_HIST = "monthly_lower_hist"
MAX_HIST = "monthly_max_hist"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _validate_identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{field} must be a non-empty string")
    if len(value) > 128:
        raise DomainError(f"{field} must be at most 128 characters")
    return value


# ---------------------------------------------------------------------------
# Samples
# ---------------------------------------------------------------------------


def _ensure_sample(
    conn: sqlite3.Connection, sample_no: str, sampling_month: Optional[str]
) -> sqlite3.Row:
    """Return the sample row, creating it when a month was supplied."""
    row = conn.execute(
        "SELECT sample_no, sampling_month, created_at FROM samples WHERE sample_no = ?",
        (sample_no,),
    ).fetchone()
    if row is not None:
        if sampling_month is not None and row["sampling_month"] != sampling_month:
            raise ConflictError(
                f"sample {sample_no!r} is fixed to sampling month "
                f"{row['sampling_month']}, not {sampling_month}"
            )
        return row

    if sampling_month is None:
        raise NotFoundError(f"sample {sample_no!r} does not exist")
    validate_month(sampling_month)
    conn.execute(
        "INSERT INTO samples(sample_no, sampling_month, created_at) VALUES (?, ?, ?)",
        (sample_no, sampling_month, _now()),
    )
    return conn.execute(
        "SELECT sample_no, sampling_month, created_at FROM samples WHERE sample_no = ?",
        (sample_no,),
    ).fetchone()


def create_sample(path: str, sample_no: str, sampling_month: str) -> tuple[dict, bool]:
    """Register a water sample with its immutable sampling month."""
    sample_no = _validate_identifier(sample_no, "sample_no")
    validate_month(sampling_month)
    with db.write_transaction(path) as conn:
        existing = conn.execute(
            "SELECT sample_no, sampling_month, created_at FROM samples WHERE sample_no = ?",
            (sample_no,),
        ).fetchone()
        if existing is not None:
            if existing["sampling_month"] != sampling_month:
                raise ConflictError(
                    f"sample {sample_no!r} is fixed to sampling month "
                    f"{existing['sampling_month']}, not {sampling_month}"
                )
            return _sample_view(conn, sample_no), False
        conn.execute(
            "INSERT INTO samples(sample_no, sampling_month, created_at) VALUES (?, ?, ?)",
            (sample_no, sampling_month, _now()),
        )
        return _sample_view(conn, sample_no), True


def get_sample(path: str, sample_no: str) -> dict:
    with db.read_transaction(path) as conn:
        return _sample_view(conn, sample_no)


def _sample_view(conn: sqlite3.Connection, sample_no: str) -> dict:
    sample = conn.execute(
        "SELECT sample_no, sampling_month, created_at FROM samples WHERE sample_no = ?",
        (sample_no,),
    ).fetchone()
    if sample is None:
        raise NotFoundError(f"sample {sample_no!r} does not exist")

    reports = conn.execute(
        "SELECT report_no, revision, kind, lower, upper, upper_inclusive, "
        "detection_limit, fingerprint, created_at "
        "FROM reports WHERE sample_no = ? ORDER BY report_no, revision",
        (sample_no,),
    ).fetchall()
    latest = {}
    for row in reports:
        previous = latest.get(row["report_no"])
        if previous is None or row["revision"] > previous:
            latest[row["report_no"]] = row["revision"]

    state = conn.execute(
        "SELECT status, lower, upper, upper_inclusive, updated_at "
        "FROM sample_state WHERE sample_no = ?",
        (sample_no,),
    ).fetchone()

    return {
        "sample_no": sample["sample_no"],
        "sampling_month": sample["sampling_month"],
        "created_at": sample["created_at"],
        "status": state["status"] if state is not None else "no_reports",
        "current_interval": _state_interval(state),
        "current_revisions": latest,
        "reports": [
            {
                "report_no": row["report_no"],
                "revision": row["revision"],
                "result": ReportContent(
                    row["kind"],
                    row["lower"],
                    row["upper"],
                    bool(row["upper_inclusive"]),
                    row["detection_limit"],
                ).to_result_dict(),
                "is_current": latest[row["report_no"]] == row["revision"],
                "fingerprint": row["fingerprint"],
                "created_at": row["created_at"],
            }
            for row in reports
        ],
        "updated_at": state["updated_at"] if state is not None else None,
    }


def _state_interval(state: Optional[sqlite3.Row]) -> Optional[dict]:
    if state is None or state["status"] != "ok":
        return None
    return Interval(
        state["lower"], state["upper"], bool(state["upper_inclusive"])
    ).to_dict()


# ---------------------------------------------------------------------------
# Report ingestion
# ---------------------------------------------------------------------------


def submit_report(
    path: str,
    sample_no: str,
    report_no: str,
    revision: int,
    content: ReportContent,
    sampling_month: Optional[str] = None,
) -> dict:
    """Store one report revision and update only this sample's aggregates.

    Rules:
    * only the highest revision of each report participates in the intersection;
    * an exact re-post (same report, revision and content) is an idempotent no-op;
    * the same report number and revision with different content is rejected;
    * a revision older than the stored maximum cannot be introduced afterwards.
    """
    sample_no = _validate_identifier(sample_no, "sample_no")
    report_no = _validate_identifier(report_no, "report_no")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise DomainError("revision must be an integer >= 1")
    if sampling_month is not None:
        validate_month(sampling_month)

    with db.write_transaction(path) as conn:
        sample = _ensure_sample(conn, sample_no, sampling_month)
        existing = conn.execute(
            "SELECT sample_no, revision, fingerprint FROM reports "
            "WHERE report_no = ? ORDER BY revision",
            (report_no,),
        ).fetchall()

        if existing and existing[0]["sample_no"] != sample_no:
            raise ConflictError(
                f"report number {report_no!r} already belongs to sample "
                f"{existing[0]['sample_no']!r}"
            )

        fingerprint = content.fingerprint()
        same_revision = next(
            (row for row in existing if row["revision"] == revision), None
        )
        if same_revision is not None:
            if same_revision["fingerprint"] != fingerprint:
                raise ConflictError(
                    f"report {report_no!r} revision {revision} already exists with "
                    "different content"
                )
            view = _sample_view(conn, sample_no)
            return {
                "accepted": False,
                "idempotent": True,
                "report_no": report_no,
                "revision": revision,
                "sample": view,
            }

        highest = existing[-1]["revision"] if existing else 0
        if revision <= highest:
            raise ConflictError(
                f"report {report_no!r} revision {revision} is not newer than the "
                f"stored highest revision {highest}"
            )

        conn.execute(
            "INSERT INTO reports(report_no, sample_no, revision, kind, lower, upper, "
            "upper_inclusive, detection_limit, fingerprint, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                report_no,
                sample_no,
                revision,
                content.kind,
                content.lower,
                content.upper,
                1 if content.upper_inclusive else 0,
                content.detection_limit,
                fingerprint,
                _now(),
            ),
        )
        _recompute_sample(conn, sample_no, sample["sampling_month"])
        view = _sample_view(conn, sample_no)
        return {
            "accepted": True,
            "idempotent": False,
            "report_no": report_no,
            "revision": revision,
            "sample": view,
        }


def _current_intervals(conn: sqlite3.Connection, sample_no: str) -> list[Interval]:
    rows = conn.execute(
        "SELECT r.lower, r.upper, r.upper_inclusive "
        "FROM reports AS r "
        "JOIN (SELECT report_no, MAX(revision) AS revision FROM reports "
        "      WHERE sample_no = ? GROUP BY report_no) AS current "
        "  ON current.report_no = r.report_no AND current.revision = r.revision "
        "WHERE r.sample_no = ? ORDER BY r.report_no",
        (sample_no, sample_no),
    ).fetchall()
    return [
        Interval(row["lower"], row["upper"], bool(row["upper_inclusive"]))
        for row in rows
    ]


def _recompute_sample(
    conn: sqlite3.Connection, sample_no: str, sampling_month: str
) -> None:
    """Replace this sample's contribution to the monthly aggregates."""
    old = conn.execute(
        "SELECT status, lower, upper, upper_inclusive FROM sample_state "
        "WHERE sample_no = ?",
        (sample_no,),
    ).fetchone()

    current = _current_intervals(conn, sample_no)
    merged: Optional[Interval] = None
    for interval in current:
        merged = interval if merged is None else intersect(merged, interval)
        if merged is None:
            break

    if old is not None:
        _remove_contribution(conn, sampling_month, old)
    else:
        _bump_summary(conn, sampling_month, total_delta=1)

    now = _now()
    if merged is None:
        _bump_summary(conn, sampling_month, retest_delta=1)
        if old is None:
            conn.execute(
                "INSERT INTO sample_state(sample_no, status, lower, upper, "
                "upper_inclusive, updated_at) VALUES (?, 'retest', NULL, NULL, NULL, ?)",
                (sample_no, now),
            )
        else:
            conn.execute(
                "UPDATE sample_state SET status = 'retest', lower = NULL, upper = NULL, "
                "upper_inclusive = NULL, updated_at = ? WHERE sample_no = ?",
                (now, sample_no),
            )
    else:
        _bump_summary(conn, sampling_month, denominator_delta=1)
        _bump_hist(conn, LOWER_HIST, sampling_month, merged.lower, 1)
        _bump_hist(conn, MAX_HIST, sampling_month, merged.max_integer, 1)
        if old is None:
            conn.execute(
                "INSERT INTO sample_state(sample_no, status, lower, upper, "
                "upper_inclusive, updated_at) VALUES (?, 'ok', ?, ?, ?, ?)",
                (
                    sample_no,
                    merged.lower,
                    merged.upper,
                    1 if merged.upper_inclusive else 0,
                    now,
                ),
            )
        else:
            conn.execute(
                "UPDATE sample_state SET status = 'ok', lower = ?, upper = ?, "
                "upper_inclusive = ?, updated_at = ? WHERE sample_no = ?",
                (
                    merged.lower,
                    merged.upper,
                    1 if merged.upper_inclusive else 0,
                    now,
                    sample_no,
                ),
            )


def _remove_contribution(
    conn: sqlite3.Connection, sampling_month: str, state: sqlite3.Row
) -> None:
    if state["status"] == "retest":
        _bump_summary(conn, sampling_month, retest_delta=-1)
        return
    interval = Interval(state["lower"], state["upper"], bool(state["upper_inclusive"]))
    _bump_summary(conn, sampling_month, denominator_delta=-1)
    _bump_hist(conn, LOWER_HIST, sampling_month, interval.lower, -1)
    _bump_hist(conn, MAX_HIST, sampling_month, interval.max_integer, -1)


def _bump_summary(
    conn: sqlite3.Connection,
    month: str,
    total_delta: int = 0,
    denominator_delta: int = 0,
    retest_delta: int = 0,
) -> None:
    # Deltas may be negative when a sample's former contribution is withdrawn.
    # Update an existing row first: unlike an upsert, this never evaluates the
    # CHECK constraints against a proposal row holding a bare negative delta.
    # A missing row with a negative delta therefore fails loudly, which aborts
    # the whole write transaction instead of corrupting the summary.
    updated = conn.execute(
        "UPDATE monthly_summary SET "
        "total_samples = total_samples + ?, "
        "denominator = denominator + ?, "
        "retest_count = retest_count + ?, "
        "updated_at = ? WHERE month = ?",
        (total_delta, denominator_delta, retest_delta, _now(), month),
    )
    if updated.rowcount == 0:
        conn.execute(
            "INSERT INTO monthly_summary(month, total_samples, denominator, "
            "retest_count, updated_at) VALUES (?, ?, ?, ?, ?)",
            (month, total_delta, denominator_delta, retest_delta, _now()),
        )


def _bump_hist(
    conn: sqlite3.Connection, table: str, month: str, value: int, delta: int
) -> None:
    if table not in (LOWER_HIST, MAX_HIST):  # internal guard against SQL injection
        raise RuntimeError("invalid histogram table")
    if value < 0:
        raise RuntimeError("histogram value must be non-negative")
    if delta > 0:
        conn.execute(
            f"INSERT INTO {table}(month, value, sample_count) VALUES (?, ?, 1) "
            "ON CONFLICT(month, value) DO UPDATE SET "
            "sample_count = sample_count + 1",
            (month, value),
        )
        return
    if delta == 0:
        return

    row = conn.execute(
        f"SELECT sample_count FROM {table} WHERE month = ? AND value = ?",
        (month, value),
    ).fetchone()
    if row is None or row["sample_count"] < 1:
        # A missing contribution means stored aggregates are inconsistent;
        # abort the transaction rather than corrupt the summary further.
        raise RuntimeError(
            f"histogram underflow for {table} month={month} value={value}"
        )
    if row["sample_count"] == 1:
        conn.execute(
            f"DELETE FROM {table} WHERE month = ? AND value = ?", (month, value)
        )
    else:
        conn.execute(
            f"UPDATE {table} SET sample_count = sample_count - 1 "
            "WHERE month = ? AND value = ?",
            (month, value),
        )


# ---------------------------------------------------------------------------
# Monthly queries
# ---------------------------------------------------------------------------


def _summarize(
    conn: sqlite3.Connection, month: str, threshold: int
) -> tuple[dict, list[str]]:
    """Read counts and retest list; caller must supply one consistent snapshot."""
    summary = conn.execute(
        "SELECT total_samples, denominator, retest_count, updated_at "
        "FROM monthly_summary WHERE month = ?",
        (month,),
    ).fetchone()

    lower_bound = conn.execute(
        f"SELECT COALESCE(SUM(sample_count), 0) AS n FROM {LOWER_HIST} "
        "WHERE month = ? AND value >= ?",
        (month, threshold),
    ).fetchone()["n"]
    upper_bound = conn.execute(
        f"SELECT COALESCE(SUM(sample_count), 0) AS n FROM {MAX_HIST} "
        "WHERE month = ? AND value >= ?",
        (month, threshold),
    ).fetchone()["n"]

    retest_samples = [
        row["sample_no"]
        for row in conn.execute(
            "SELECT s.sample_no FROM sample_state AS st "
            "JOIN samples AS s ON s.sample_no = st.sample_no "
            "WHERE st.status = 'retest' AND s.sampling_month = ? "
            "ORDER BY s.sample_no",
            (month,),
        ).fetchall()
    ]

    result = {
        "month": month,
        "threshold": threshold,
        "total_samples": summary["total_samples"] if summary else 0,
        "denominator": summary["denominator"] if summary else 0,
        "at_or_above_lower_bound": lower_bound,
        "at_or_above_upper_bound": upper_bound,
        "retest_count": summary["retest_count"] if summary else 0,
        "retest_samples": retest_samples,
        "calculated_at": _now(),
    }
    _check_summary_invariants(result)
    return result, retest_samples


def _check_summary_invariants(result: dict) -> None:
    """Guard the derived counters; a violation means corrupted aggregates."""
    total = result["total_samples"]
    denominator = result["denominator"]
    retest_count = result["retest_count"]
    lower = result["at_or_above_lower_bound"]
    upper = result["at_or_above_upper_bound"]
    if denominator + retest_count != total:
        raise RuntimeError("monthly summary totals are inconsistent")
    if retest_count != len(result["retest_samples"]):
        raise RuntimeError("monthly summary and retest list are inconsistent")
    if not 0 <= lower <= upper <= denominator:
        raise RuntimeError("monthly bound counts are inconsistent")


def month_summary(path: str, month: str, threshold: int) -> dict:
    """Return bound counts and the retest list from a single committed snapshot."""
    validate_month(month, "month")
    validate_threshold(threshold)
    with db.read_transaction(path) as conn:
        result, _ = _summarize(conn, month, threshold)
    return result


def retest_samples(path: str, month: str) -> dict:
    """Return only the samples awaiting retest for one month."""
    validate_month(month, "month")
    with db.read_transaction(path) as conn:
        summary = conn.execute(
            "SELECT retest_count FROM monthly_summary WHERE month = ?", (month,)
        ).fetchone()
        samples = [
            row["sample_no"]
            for row in conn.execute(
                "SELECT s.sample_no FROM sample_state AS st "
                "JOIN samples AS s ON s.sample_no = st.sample_no "
                "WHERE st.status = 'retest' AND s.sampling_month = ? "
                "ORDER BY s.sample_no",
                (month,),
            ).fetchall()
        ]
    if summary and summary["retest_count"] != len(samples):
        raise RuntimeError("monthly summary and retest list are inconsistent")
    return {
        "month": month,
        "retest_count": summary["retest_count"] if summary else 0,
        "retest_samples": samples,
    }
