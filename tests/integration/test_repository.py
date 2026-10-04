import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.domain.models import BatchStatus, RowInput, RowStatus
from app.persistence.repository import ResumeClaim

ROWS = [RowInput(1, "A", "Addr", None), RowInput(2, "B", "Addr", "555")]


async def new_batch(repo, rows=ROWS):
    batch_id = uuid4()
    await repo.create_batch(batch_id, rows)
    return batch_id


async def set_row(engine, batch_id, row_no, **values):
    assignments = ", ".join(f"{k} = :{k}" for k in values)
    async with engine.begin() as conn:
        await conn.execute(
            text(f"UPDATE batch_job_row SET {assignments} WHERE batch_id = :b AND row_no = :n"),
            {"b": batch_id, "n": row_no, **values},
        )


class TestResumeClaim:
    async def test_two_concurrent_resumes_exactly_one_wins(self, repo):
        batch_id = await new_batch(repo)
        await repo.stop_batch(batch_id, BatchStatus.FAILED, "boom")

        results = await asyncio.gather(*[repo.claim_for_resume(batch_id) for _ in range(5)])

        claims = sorted(claim.value for claim, _ in results)
        assert claims == ["claimed"] + ["not_resumable"] * 4
        assert (await repo.get_batch(batch_id)).status is BatchStatus.PROCESSING

    async def test_activation_failed_resumes_into_activating(self, repo):
        batch_id = await new_batch(repo)
        await repo.stop_batch(batch_id, BatchStatus.ACTIVATION_FAILED, "boom")

        assert await repo.claim_for_resume(batch_id) == (ResumeClaim.CLAIMED, BatchStatus.ACTIVATING)

    async def test_resume_gives_exhausted_rows_a_fresh_budget_and_leaves_unknown_rows_alone(self, repo, engine):
        batch_id = await new_batch(repo)
        await set_row(engine, batch_id, 1, status="retry_exhausted", attempts=4)
        await set_row(engine, batch_id, 2, status="unknown", attempts=1)
        await repo.stop_batch(batch_id, BatchStatus.FAILED, "boom")

        await repo.claim_for_resume(batch_id)

        rows = await repo.get_rows(batch_id)
        assert [(r.status, r.attempts) for r in rows] == [(RowStatus.PENDING, 0), (RowStatus.UNKNOWN, 1)]

    async def test_batch_with_rejected_rows_cannot_resume(self, repo, engine):
        batch_id = await new_batch(repo)
        await set_row(engine, batch_id, 1, status="rejected")
        await repo.stop_batch(batch_id, BatchStatus.FAILED, "boom")

        assert await repo.claim_for_resume(batch_id) == (ResumeClaim.HAS_REJECTED_ROWS, BatchStatus.FAILED)

    async def test_running_batch_cannot_resume(self, repo):
        batch_id = await new_batch(repo)

        assert await repo.claim_for_resume(batch_id) == (ResumeClaim.NOT_RESUMABLE, BatchStatus.PROCESSING)

    async def test_unknown_batch(self, repo):
        assert await repo.claim_for_resume(uuid4()) == (ResumeClaim.NOT_FOUND, None)


class TestSweeper:
    async def test_stale_batch_is_interrupted_and_in_flight_rows_become_unknown(self, repo, engine):
        stale, fresh = await new_batch(repo), await new_batch(repo)
        await repo.mark_in_flight(stale, 1)
        await repo.mark_in_flight(fresh, 1)
        async with engine.begin() as conn:
            await conn.execute(
                text("UPDATE batch_job SET last_heartbeat_at = now() - interval '1 minute' WHERE batch_id = :b"),
                {"b": stale},
            )

        assert await repo.sweep_stale(30) == [stale]

        assert (await repo.get_batch(stale)).status is BatchStatus.INTERRUPTED
        assert [r.status for r in await repo.get_rows(stale)] == [RowStatus.UNKNOWN, RowStatus.PENDING]
        assert (await repo.get_batch(fresh)).status is BatchStatus.PROCESSING

    async def test_swept_batch_stops_heartbeat_and_refuses_new_sends(self, repo):
        batch_id = await new_batch(repo)
        await repo.stop_batch(batch_id, BatchStatus.INTERRUPTED, "swept")

        assert await repo.heartbeat(batch_id) is False
        assert await repo.mark_in_flight(batch_id, 1) is None


class TestRowGuards:
    async def test_outcome_is_dropped_if_row_is_no_longer_in_flight(self, repo):
        batch_id = await new_batch(repo)
        await repo.mark_in_flight(batch_id, 1)
        await repo.stop_batch(batch_id, BatchStatus.INTERRUPTED, "swept")  # row 1 -> unknown

        await repo.record_row_outcome(batch_id, 1, RowStatus.CREATED, hospital_id=7)

        assert (await repo.get_rows(batch_id))[0].status is RowStatus.UNKNOWN

    async def test_database_refuses_created_without_upstream_id(self, repo, engine):
        batch_id = await new_batch(repo)

        with pytest.raises(IntegrityError):
            await set_row(engine, batch_id, 1, status="created")

    async def test_activation_requires_every_row_created(self, repo):
        batch_id = await new_batch(repo)
        await repo.mark_in_flight(batch_id, 1)
        await repo.record_row_outcome(batch_id, 1, RowStatus.CREATED, hospital_id=7)

        assert await repo.start_activation(batch_id) is False
