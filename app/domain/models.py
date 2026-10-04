from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class BatchStatus(StrEnum):
    # a batch is inserted directly as PROCESSING with a heartbeat, so a crash before the runner
    # starts is caught by the ordinary stale-heartbeat sweep; no separate QUEUED state is needed
    PROCESSING = "processing"
    ACTIVATING = "activating"
    COMPLETED = "completed"
    FAILED = "failed"
    ACTIVATION_FAILED = "activation_failed"
    INTERRUPTED = "interrupted"


RESUMABLE_STATUSES = (BatchStatus.FAILED, BatchStatus.INTERRUPTED, BatchStatus.ACTIVATION_FAILED)


class RowStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    CREATED = "created"
    UNKNOWN = "unknown"  # request may or may not have been committed upstream; reconcile before retrying
    RETRY_EXHAUSTED = "retry_exhausted"
    REJECTED = "rejected"  # upstream refused the data; terminal


TERMINAL_ROW_STATUSES = (RowStatus.CREATED, RowStatus.REJECTED, RowStatus.RETRY_EXHAUSTED)


@dataclass(frozen=True)
class RowInput:
    row_no: int  # 1-based index among data rows, excluding the header
    name: str
    address: str
    phone: str | None


@dataclass(frozen=True)
class RowRecord:
    row_no: int
    name: str
    address: str
    phone: str | None
    status: RowStatus
    upstream_hospital_id: int | None
    attempts: int
    last_error: str | None


@dataclass(frozen=True)
class BatchRecord:
    batch_id: UUID
    status: BatchStatus
    total_rows: int
    processing_seconds: float | None  # wall-clock from first start to completion (or now, if running)
    last_error: str | None
