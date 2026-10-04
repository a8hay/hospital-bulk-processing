"""Our API's contract. Deliberately separate from the upstream client's models."""

from uuid import UUID

from pydantic import BaseModel

from app.domain.models import BatchStatus


class BatchAccepted(BaseModel):
    batch_id: UUID
    status: BatchStatus
    total_hospitals: int
    status_url: str


class HospitalResult(BaseModel):
    row: int
    hospital_id: int | None
    name: str
    status: str
    error: str | None = None


class BatchResult(BaseModel):
    """The spec's response shape, plus `status`, `pending_hospitals` and `error` for async progress."""

    batch_id: UUID
    status: BatchStatus
    total_hospitals: int
    processed_hospitals: int  # created upstream
    failed_hospitals: int  # rejected or out of retries
    pending_hospitals: int  # not yet settled (pending, in flight, or unknown awaiting reconciliation)
    processing_time_seconds: float
    batch_activated: bool
    error: str | None
    hospitals: list[HospitalResult]


class CsvIssue(BaseModel):
    message: str
    row: int | None
    line: int | None
    column: str | None


class CsvInvalid(BaseModel):
    detail: str = "invalid CSV"
    errors: list[CsvIssue]


class CsvReport(BaseModel):
    valid: bool
    total_hospitals: int
    errors: list[CsvIssue]


class Problem(BaseModel):
    detail: str
