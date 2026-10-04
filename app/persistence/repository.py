"""All SQL lives here.

Every state transition is a guarded UPDATE: it names the state it expects to move from, so a
writer that lost a race (a second resume, a runner that was swept) changes nothing instead of
overwriting newer state. All timestamps come from the database clock.
"""

from collections.abc import Sequence
from enum import Enum
from importlib import resources
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.domain.models import (
    RESUMABLE_STATUSES,
    TERMINAL_ROW_STATUSES,
    BatchRecord,
    BatchStatus,
    RowInput,
    RowRecord,
    RowStatus,
)


class ResumeClaim(Enum):
    CLAIMED = "claimed"
    NOT_FOUND = "not_found"
    NOT_RESUMABLE = "not_resumable"  # running or completed
    HAS_REJECTED_ROWS = "has_rejected_rows"  # resuming cannot help; the data must change


async def apply_schema(engine: AsyncEngine) -> None:
    sql = resources.files("app.persistence").joinpath("schema.sql").read_text()
    statements = [s.strip() for s in sql.split(";") if s.strip()]
    async with engine.begin() as conn:
        for statement in statements:
            await conn.exec_driver_sql(statement)


_CLAIM_FOR_RESUME = text("""
    UPDATE batch_job
    SET    status = CASE WHEN status = 'activation_failed' THEN 'activating' ELSE 'processing' END,
           last_heartbeat_at = now(), completed_at = NULL, last_error = NULL
    WHERE  batch_id = :batch_id
      AND  status IN ('failed', 'interrupted', 'activation_failed')
      AND  NOT EXISTS (SELECT 1 FROM batch_job_row WHERE batch_id = :batch_id AND status = 'rejected')
    RETURNING status
""")

_RESET_EXHAUSTED_ROWS = text("""
    UPDATE batch_job_row
    SET    status = 'pending', attempts = 0, last_error = NULL, completed_at = NULL
    WHERE  batch_id = :batch_id AND status = 'retry_exhausted'
""")

# Moving the batch and its in-flight rows in one statement means a crash can never leave an
# interrupted batch whose rows still claim to be in flight (which resume might re-POST blindly).
_STOP_BATCH = text("""
    WITH stopped AS (
        UPDATE batch_job
        SET    status = CAST(:status AS TEXT), last_error = :error, completed_at = now()
        WHERE  batch_id = :batch_id AND status IN ('processing', 'activating')
        RETURNING batch_id
    )
    UPDATE batch_job_row r SET status = 'unknown'
    FROM   stopped
    WHERE  r.batch_id = stopped.batch_id AND r.status = 'in_flight'
""")

_SWEEP_STALE = text("""
    WITH stale AS (
        UPDATE batch_job
        SET    status = 'interrupted', last_error = 'runner stopped heartbeating', completed_at = now()
        WHERE  status IN ('processing', 'activating')
          AND  last_heartbeat_at < now() - make_interval(secs => :stale_after)
        RETURNING batch_id
    ),
    unknown_rows AS (
        UPDATE batch_job_row r SET status = 'unknown'
        FROM   stale
        WHERE  r.batch_id = stale.batch_id AND r.status = 'in_flight'
    )
    SELECT batch_id FROM stale
""")


def _row(record) -> RowRecord:
    return RowRecord(
        row_no=record.row_no,
        name=record.name,
        address=record.address,
        phone=record.phone,
        status=RowStatus(record.status),
        upstream_hospital_id=record.upstream_hospital_id,
        attempts=record.attempts,
        last_error=record.last_error,
    )


class Repository:
    def __init__(self, engine: AsyncEngine):
        self._engine = engine

    async def create_batch(self, batch_id: UUID, rows: Sequence[RowInput]) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO batch_job (batch_id, status, total_rows) VALUES (:batch_id, 'processing', :total)"),
                {"batch_id": batch_id, "total": len(rows)},
            )
            await conn.execute(
                text("""
                    INSERT INTO batch_job_row (batch_id, row_no, name, address, phone, status)
                    VALUES (:batch_id, :row_no, :name, :address, :phone, 'pending')
                """),
                [
                    {"batch_id": batch_id, "row_no": r.row_no, "name": r.name, "address": r.address, "phone": r.phone}
                    for r in rows
                ],
            )

    async def get_batch(self, batch_id: UUID) -> BatchRecord | None:
        async with self._engine.connect() as conn:
            record = (
                await conn.execute(
                    text("""
                        SELECT batch_id, status, total_rows, last_error,
                               EXTRACT(EPOCH FROM coalesce(completed_at, now()) - started_at) AS processing_seconds
                        FROM   batch_job WHERE batch_id = :batch_id
                    """),
                    {"batch_id": batch_id},
                )
            ).first()
        if record is None:
            return None
        return BatchRecord(
            batch_id=record.batch_id,
            status=BatchStatus(record.status),
            total_rows=record.total_rows,
            processing_seconds=float(record.processing_seconds),
            last_error=record.last_error,
        )

    async def get_rows(self, batch_id: UUID, statuses: Sequence[RowStatus] | None = None) -> list[RowRecord]:
        query = "SELECT * FROM batch_job_row WHERE batch_id = :batch_id"
        params: dict = {"batch_id": batch_id}
        if statuses is not None:
            query += " AND status = ANY(CAST(:statuses AS TEXT[]))"
            params["statuses"] = [str(s) for s in statuses]
        async with self._engine.connect() as conn:
            result = await conn.execute(text(query + " ORDER BY row_no"), params)
            return [_row(r) for r in result]

    async def claim_for_resume(self, batch_id: UUID) -> tuple[ResumeClaim, BatchStatus | None]:
        async with self._engine.begin() as conn:
            # Concurrent resumes: the second UPDATE blocks on the row lock, then re-checks its WHERE
            # against the committed row, sees 'processing', and matches nothing.
            claimed = (await conn.execute(_CLAIM_FOR_RESUME, {"batch_id": batch_id})).scalar_one_or_none()
            if claimed is not None:
                await conn.execute(_RESET_EXHAUSTED_ROWS, {"batch_id": batch_id})
                return ResumeClaim.CLAIMED, BatchStatus(claimed)

            # Only explains the refusal; a race here can change the message, never the outcome.
            status = (
                await conn.execute(text("SELECT status FROM batch_job WHERE batch_id = :batch_id"), {"batch_id": batch_id})
            ).scalar_one_or_none()
        if status is None:
            return ResumeClaim.NOT_FOUND, None
        status = BatchStatus(status)
        if status in RESUMABLE_STATUSES:
            return ResumeClaim.HAS_REJECTED_ROWS, status
        return ResumeClaim.NOT_RESUMABLE, status

    async def heartbeat(self, batch_id: UUID) -> bool:
        """Returns False if the batch is no longer ours to run (it was swept or finished)."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("""
                    UPDATE batch_job SET last_heartbeat_at = now()
                    WHERE  batch_id = :batch_id AND status IN ('processing', 'activating')
                    RETURNING batch_id
                """),
                {"batch_id": batch_id},
            )
            return result.first() is not None

    async def mark_in_flight(self, batch_id: UUID, row_no: int) -> int | None:
        """Claim a pending row for one POST. Returns the attempt number, or None if we must not send.

        The attempt is counted before sending, so one interrupted by a crash still counts. The batch
        must still be processing, so a runner that was swept stops sending immediately.
        """
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("""
                    UPDATE batch_job_row
                    SET    status = 'in_flight', attempts = attempts + 1, started_at = coalesce(started_at, now())
                    WHERE  batch_id = :batch_id AND row_no = :row_no AND status = 'pending'
                      AND  EXISTS (SELECT 1 FROM batch_job WHERE batch_id = :batch_id AND status = 'processing')
                    RETURNING attempts
                """),
                {"batch_id": batch_id, "row_no": row_no},
            )
            return result.scalar_one_or_none()

    async def record_row_outcome(
        self,
        batch_id: UUID,
        row_no: int,
        status: RowStatus,
        hospital_id: int | None = None,
        error: str | None = None,
    ) -> None:
        # Guarded on in_flight: if the sweeper already moved this row to unknown, our write is
        # dropped, and reconciliation will adopt the hospital we created anyway.
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                    UPDATE batch_job_row
                    SET    status = CAST(:status AS TEXT), upstream_hospital_id = :hospital_id, last_error = :error,
                           completed_at = CASE WHEN CAST(:terminal AS BOOLEAN) THEN now() END
                    WHERE  batch_id = :batch_id AND row_no = :row_no AND status = 'in_flight'
                """),
                {
                    "batch_id": batch_id,
                    "row_no": row_no,
                    "status": str(status),
                    "hospital_id": hospital_id,
                    "error": error,
                    "terminal": status in TERMINAL_ROW_STATUSES,
                },
            )

    async def apply_reconciliation(self, batch_id: UUID, matches: dict[int, int | None], max_attempts: int) -> None:
        found = [{"batch_id": batch_id, "row_no": n, "hospital_id": h} for n, h in matches.items() if h is not None]
        missing = [{"batch_id": batch_id, "row_no": n, "max": max_attempts} for n, h in matches.items() if h is None]
        async with self._engine.begin() as conn:
            if found:
                await conn.execute(
                    text("""
                        UPDATE batch_job_row
                        SET    status = 'created', upstream_hospital_id = :hospital_id, last_error = NULL,
                               completed_at = now()
                        WHERE  batch_id = :batch_id AND row_no = :row_no AND status = 'unknown'
                    """),
                    found,
                )
            if missing:
                # verified absent upstream, so sending again is safe, if the row has attempts left
                await conn.execute(
                    text("""
                        UPDATE batch_job_row
                        SET    status = CASE WHEN attempts >= :max THEN 'retry_exhausted' ELSE 'pending' END,
                               completed_at = CASE WHEN attempts >= :max THEN now() END
                        WHERE  batch_id = :batch_id AND row_no = :row_no AND status = 'unknown'
                    """),
                    missing,
                )

    async def start_activation(self, batch_id: UUID) -> bool:
        async with self._engine.begin() as conn:
            result = await conn.execute(
                text("""
                    UPDATE batch_job SET status = 'activating'
                    WHERE  batch_id = :batch_id AND status = 'processing'
                      AND  NOT EXISTS (SELECT 1 FROM batch_job_row WHERE batch_id = :batch_id AND status <> 'created')
                    RETURNING batch_id
                """),
                {"batch_id": batch_id},
            )
            return result.first() is not None

    async def complete_batch(self, batch_id: UUID) -> None:
        async with self._engine.begin() as conn:
            await conn.execute(
                text("""
                    UPDATE batch_job
                    SET    status = 'completed', activated_at = now(), completed_at = now(), last_error = NULL
                    WHERE  batch_id = :batch_id AND status = 'activating'
                """),
                {"batch_id": batch_id},
            )

    async def stop_batch(self, batch_id: UUID, status: BatchStatus, error: str) -> None:
        """Move a running batch to failed / activation_failed / interrupted; in-flight rows become unknown."""
        async with self._engine.begin() as conn:
            await conn.execute(_STOP_BATCH, {"batch_id": batch_id, "status": str(status), "error": error})

    async def sweep_stale(self, stale_after_seconds: float) -> list[UUID]:
        async with self._engine.begin() as conn:
            result = await conn.execute(_SWEEP_STALE, {"stale_after": stale_after_seconds})
            return [r.batch_id for r in result]
