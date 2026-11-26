"""FastAPI application for the single-section total-phosphorus check."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal, Optional, Union

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from . import db, service
from .domain import ConflictError, DomainError, NotFoundError, ReportContent


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SampleCreate(BaseModel):
    sample_no: str = Field(..., min_length=1, max_length=128, examples=["S-2026-001"])
    sampling_month: str = Field(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=["2026-11"],
        description="Fixed sampling month; cannot change after the sample is created.",
    )

    model_config = {
        "json_schema_extra": {
            "examples": [{"sample_no": "S-2026-001", "sampling_month": "2026-11"}]
        }
    }


class MeasuredResult(BaseModel):
    kind: Literal["measured"] = "measured"
    lower: int = Field(..., ge=0, examples=[30], description="Inclusive lower bound, ug/L.")
    upper: int = Field(..., ge=0, examples=[45], description="Inclusive upper bound, ug/L.")

    @model_validator(mode="after")
    def _ordered(self) -> "MeasuredResult":
        if self.lower > self.upper:
            raise ValueError("lower must be <= upper")
        return self


class NonDetectResult(BaseModel):
    kind: Literal["nondetect"] = "nondetect"
    detection_limit: int = Field(
        ...,
        gt=0,
        examples=[10],
        description="Detection limit L, ug/L. Interpreted as the interval [0, L).",
    )


ReportResult = Union[MeasuredResult, NonDetectResult]


class ReportSubmit(BaseModel):
    report_no: str = Field(..., min_length=1, max_length=128, examples=["LAB-A-77"])
    revision: int = Field(..., ge=1, examples=[1])
    result: ReportResult = Field(
        ...,
        discriminator="kind",
        description=(
            "A measured closed interval [lower, upper] or a non-detect limit L "
            "meaning [0, L)."
        ),
    )
    sampling_month: Optional[str] = Field(
        default=None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
        examples=[None],
        description=(
            "Optional month used only when this request creates the sample; it must "
            "match the sample's fixed month otherwise."
        ),
    )

    def content(self) -> ReportContent:
        if self.result.kind == "measured":
            return ReportContent.measured(self.result.lower, self.result.upper)
        return ReportContent.nondetect(self.result.detection_limit)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "report_no": "LAB-A-77",
                    "revision": 1,
                    "result": {"kind": "measured", "lower": 30, "upper": 45},
                },
                {
                    "report_no": "LAB-B-12",
                    "revision": 2,
                    "result": {"kind": "nondetect", "detection_limit": 10},
                },
            ]
        }
    }


class ErrorBody(BaseModel):
    detail: str


class IntervalOut(BaseModel):
    lower: int
    upper: int
    upper_inclusive: bool
    notation: str


class ReportOut(BaseModel):
    report_no: str
    revision: int
    result: dict
    is_current: bool
    fingerprint: str
    created_at: str


class SampleOut(BaseModel):
    sample_no: str
    sampling_month: str
    created_at: str
    status: Literal["ok", "retest", "no_reports"]
    current_interval: Optional[IntervalOut]
    current_revisions: dict[str, int]
    reports: list[ReportOut]
    updated_at: Optional[str]


class ReportIngestOut(BaseModel):
    accepted: bool
    idempotent: bool
    report_no: str
    revision: int
    sample: SampleOut


class SummaryOut(BaseModel):
    month: str
    threshold: int
    total_samples: int = Field(..., description="Samples with at least one lab report.")
    denominator: int = Field(
        ..., description="Samples whose current interval is non-empty."
    )
    at_or_above_lower_bound: int = Field(
        ...,
        description=(
            "Samples that certainly reach or exceed the warning line: every value in "
            "their current interval is >= threshold."
        ),
    )
    at_or_above_upper_bound: int = Field(
        ...,
        description=(
            "Samples that may reach or exceed the warning line: at least one value in "
            "their current interval is >= threshold."
        ),
    )
    retest_count: int
    retest_samples: list[str]
    calculated_at: str


class RetestOut(BaseModel):
    month: str
    retest_count: int
    retest_samples: list[str]


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(db_path: Optional[str] = None) -> FastAPI:
    path = db.database_path(db_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.initialize(path)
        yield

    app = FastAPI(
        title="Single-section total phosphorus check",
        version="1.0.0",
        description=(
            "Lab reports declare integer concentration intervals in ug/L "
            "(measured `[lower, upper]`, non-detect `[0, detection_limit)`). "
            "Each report is used at its highest revision; a water sample's current "
            "interval is the intersection of its current report intervals. Empty "
            "intersections are listed for retest and excluded from the denominator."
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(NotFoundError)
    async def _not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def _conflict(_: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(DomainError)
    async def _domain_error(_: Request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(sqlite3.DatabaseError)
    async def _db_error(_: Request, exc: sqlite3.DatabaseError) -> JSONResponse:
        # The failing transaction has already been rolled back; readers see the
        # last committed state. Never leak storage details to the client.
        logging.getLogger("tp-check").exception("database operation failed")
        return JSONResponse(
            status_code=500,
            content={"detail": "storage failure; previous results remain in effect"},
        )

    @app.get("/healthz", tags=["ops"], summary="Liveness check")
    def healthz() -> dict:
        return {"status": "ok"}

    @app.post(
        "/samples",
        response_model=SampleOut,
        status_code=201,
        tags=["samples"],
        summary="Register a water sample with its fixed sampling month",
        responses={409: {"model": ErrorBody, "description": "Month conflict"}},
    )
    def create_sample(body: SampleCreate) -> dict:
        sample, _ = service.create_sample(path, body.sample_no, body.sampling_month)
        return sample

    @app.get(
        "/samples/{sample_no}",
        response_model=SampleOut,
        tags=["samples"],
        summary="Read a sample, its report history and current interval",
        responses={404: {"model": ErrorBody}},
    )
    def get_sample(sample_no: str) -> dict:
        return service.get_sample(path, sample_no)

    @app.post(
        "/samples/{sample_no}/reports",
        response_model=ReportIngestOut,
        status_code=201,
        tags=["reports"],
        summary="Submit or correct one lab report revision",
        description=(
            "A new revision is created with status 201. Re-posting the identical "
            "report number, revision and content is idempotent and returns 200 "
            "without adding a report, re-counting or recomputing the summary."
        ),
        responses={
            200: {
                "model": ReportIngestOut,
                "description": "Exact re-post; nothing changed",
            },
            404: {"model": ErrorBody},
            409: {
                "model": ErrorBody,
                "description": (
                    "Same report number/revision with different content, a stale "
                    "revision, or a report number bound to another sample"
                ),
            },
            422: {"model": ErrorBody, "description": "Invalid interval or month"},
        },
    )
    def submit_report(sample_no: str, body: ReportSubmit, response: Response) -> dict:
        outcome = service.submit_report(
            path,
            sample_no=sample_no,
            report_no=body.report_no,
            revision=body.revision,
            content=body.content(),
            sampling_month=body.sampling_month,
        )
        if outcome["idempotent"]:
            response.status_code = 200
        return outcome

    @app.get(
        "/summary",
        response_model=SummaryOut,
        tags=["summary"],
        summary="Monthly count bounds, denominator and retest list",
        description=(
            "Counts and the retest list are read from one committed SQLite snapshot, "
            "so a concurrent correction cannot make them disagree."
        ),
    )
    def summary(
        month: str = Query(..., examples=["2026-11"], description="Sampling month YYYY-MM."),
        threshold: int = Query(
            ...,
            examples=[20],
            description="Warning line in integer ug/L; reached means >= threshold.",
        ),
    ) -> dict:
        return service.month_summary(path, month, threshold)

    @app.get(
        "/retest",
        response_model=RetestOut,
        tags=["summary"],
        summary="Samples awaiting retest for a month",
    )
    def retest(
        month: str = Query(..., examples=["2026-11"], description="Sampling month YYYY-MM."),
    ) -> dict:
        return service.retest_samples(path, month)

    app.state.db_path = path
    return app


app = create_app()
