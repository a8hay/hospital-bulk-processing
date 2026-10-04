from dataclasses import dataclass
from enum import StrEnum


class BatchStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    ACTIVATING = "activating"
    COMPLETED = "completed"
    FAILED = "failed"
    ACTIVATION_FAILED = "activation_failed"
    INTERRUPTED = "interrupted"


class RowStatus(StrEnum):
    PENDING = "pending"
    IN_FLIGHT = "in_flight"
    CREATED = "created"
    UNKNOWN = "unknown"  # request may or may not have been committed upstream; reconcile before retrying
    RETRY_EXHAUSTED = "retry_exhausted"
    REJECTED = "rejected"  # upstream refused the data; terminal


@dataclass(frozen=True)
class RowInput:
    row_no: int  # 1-based index among data rows, excluding the header
    name: str
    address: str
    phone: str | None
