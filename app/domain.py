"""Domain rules for total-phosphorus (TP) laboratory reports.

Concentrations are integers in micrograms per litre (ug/L). Every lab report
declares where the true concentration must lie:

* a measured report declares a *closed* interval ``[lower, upper]``;
* a non-detect report declares a *half-open* interval ``[0, detection_limit)``.

Because concentrations are integers, a half-open interval ``[0, L)`` is
equivalent to the inclusive integer range ``[0, L-1]``. The domain keeps the
half-open flag so API output preserves the original meaning, while comparison
and histogram logic use the largest reachable integer.

A water sample's current interval is the intersection of the current
(highest-version) interval of every report attached to it. An empty
intersection marks the sample for retesting and removes it from denominators.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional


class DomainError(ValueError):
    """A request violates a laboratory-report invariant."""


class NotFoundError(DomainError):
    """The referenced water sample does not exist."""


class ConflictError(DomainError):
    """The request contradicts already stored immutable data."""


_MONTH_RE = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")

# SQLite stores INTEGER as a signed 64-bit value; larger Python ints would be
# coerced to REAL and silently lose the exact integer semantics we rely on.
MAX_CONCENTRATION = 2**63 - 1


def _check_endpoint(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise DomainError(f"{field} must be an integer number of ug/L")
    if value < 0:
        raise DomainError(f"{field} must not be negative")
    if value > MAX_CONCENTRATION:
        raise DomainError(f"{field} exceeds the supported integer range")
    return value


def validate_month(value: str, field: str = "sampling_month") -> str:
    """Validate the fixed sampling month in ``YYYY-MM`` form."""
    if not isinstance(value, str) or not _MONTH_RE.fullmatch(value):
        raise DomainError(f"{field} must use YYYY-MM with a month from 01 to 12")
    return value


def validate_threshold(value: int) -> int:
    return _check_endpoint(value, "threshold")


@dataclass(frozen=True)
class Interval:
    """A concentration interval with an inclusive lower bound.

    ``upper`` is inclusive when ``upper_inclusive`` is true. A non-detect
    ``[0, L)`` is represented as ``Interval(0, L, upper_inclusive=False)``.
    """

    lower: int
    upper: int
    upper_inclusive: bool = True

    def __post_init__(self) -> None:
        _check_endpoint(self.lower, "lower")
        _check_endpoint(self.upper, "upper")
        if self.upper < self.lower:
            raise DomainError("interval upper endpoint must be >= lower endpoint")
        if not self.upper_inclusive and self.upper <= self.lower:
            # [0, 0) contains no integer at all.
            raise DomainError("half-open interval must contain at least one integer")

    @property
    def max_integer(self) -> int:
        """Largest integer contained in the interval."""
        return self.upper if self.upper_inclusive else self.upper - 1

    @property
    def is_empty(self) -> bool:
        return self.max_integer < self.lower

    @property
    def notation(self) -> str:
        right = "]" if self.upper_inclusive else ")"
        return f"[{self.lower}, {self.upper}{right}"

    def guaranteed_at_or_above(self, threshold: int) -> bool:
        """Every value in the interval is >= threshold."""
        return self.lower >= threshold

    def can_reach_or_exceed(self, threshold: int) -> bool:
        """At least one value in the interval is >= threshold."""
        return self.max_integer >= threshold

    def to_dict(self) -> dict:
        return {
            "lower": self.lower,
            "upper": self.upper,
            "upper_inclusive": self.upper_inclusive,
            "notation": self.notation,
        }


def intersect(left: Interval, right: Interval) -> Optional[Interval]:
    """Intersect two intervals; return ``None`` when no integer remains."""
    lower = max(left.lower, right.lower)

    if left.upper < right.upper:
        upper, upper_inclusive = left.upper, left.upper_inclusive
    elif right.upper < left.upper:
        upper, upper_inclusive = right.upper, right.upper_inclusive
    else:
        # Same numeric bound: an exclusive bound is the tighter one.
        upper = left.upper
        upper_inclusive = left.upper_inclusive and right.upper_inclusive

    if upper_inclusive:
        if lower > upper:
            return None
    elif lower >= upper:
        return None
    return Interval(lower, upper, upper_inclusive)


@dataclass(frozen=True)
class ReportContent:
    """Canonical, normalised content of one lab report revision."""

    kind: str
    lower: int
    upper: int
    upper_inclusive: bool
    detection_limit: Optional[int] = None

    @classmethod
    def measured(cls, lower: int, upper: int) -> "ReportContent":
        _check_endpoint(lower, "lower")
        _check_endpoint(upper, "upper")
        if lower > upper:
            raise DomainError("measured lower endpoint must be <= upper endpoint")
        return cls("measured", lower, upper, True, None)

    @classmethod
    def nondetect(cls, detection_limit: int) -> "ReportContent":
        _check_endpoint(detection_limit, "detection_limit")
        if detection_limit <= 0:
            raise DomainError("detection_limit must be a positive integer")
        return cls("nondetect", 0, detection_limit, False, detection_limit)

    def __post_init__(self) -> None:
        if self.kind not in ("measured", "nondetect"):
            raise DomainError("kind must be 'measured' or 'nondetect'")
        if not isinstance(self.upper_inclusive, bool):
            raise DomainError("upper_inclusive must be a boolean")
        if self.kind == "measured":
            if not self.upper_inclusive or self.detection_limit is not None:
                raise DomainError("measured content is inconsistent")
        else:
            if (
                self.lower != 0
                or self.upper_inclusive
                or self.detection_limit != self.upper
                or self.upper <= 0
            ):
                raise DomainError("non-detect content is inconsistent")
        self.interval  # validates integer type, range and ordering

    @property
    def interval(self) -> Interval:
        return Interval(self.lower, self.upper, self.upper_inclusive)

    def to_result_dict(self) -> dict:
        if self.kind == "measured":
            return {"kind": "measured", "lower": self.lower, "upper": self.upper}
        return {"kind": "nondetect", "detection_limit": self.detection_limit}

    def fingerprint(self) -> str:
        payload = json.dumps(
            {
                "kind": self.kind,
                "lower": self.lower,
                "upper": self.upper,
                "upper_inclusive": self.upper_inclusive,
                "detection_limit": self.detection_limit,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()
