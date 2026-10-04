"""Drives one batch from its current state to a terminal one.

The runner owns everything that happens between upstream calls: attempt counting, backoff,
reconciliation and the heartbeat. It knows nothing about HTTP routes; callers claim a batch
(insert or resume) and then hand it here.

Invariant: never send a request whose effect may already exist upstream. Rows whose outcome is
unknown are reconciled against the upstream batch before anything is re-sent.
"""

import asyncio
import logging
import random
from collections.abc import Callable
from uuid import UUID

from app.config import Settings
from app.domain.backoff import backoff_delay
from app.domain.models import BatchStatus, RowRecord, RowStatus
from app.domain.reconcile import ActivationState, activation_state, match_unknown_rows
from app.integration.hospital_client import (
    Activated,
    Created,
    HospitalClient,
    Rejected,
    Retryable,
    Unknown,
    UpstreamUnavailable,
)
from app.persistence.repository import Repository

log = logging.getLogger(__name__)


class Runner:
    def __init__(
        self,
        repo: Repository,
        client: HospitalClient,
        settings: Settings,
        rand: Callable[[], float] = random.random,
    ):
        self._repo = repo
        self._client = client
        self._settings = settings
        self._rand = rand

    async def run(self, batch_id: UUID) -> None:
        work = asyncio.create_task(self._run(batch_id))
        beat = asyncio.create_task(self._heartbeat(batch_id, work))
        try:
            await work
        except asyncio.CancelledError:
            if beat.done() and not beat.cancelled():
                log.warning("batch %s was swept while running; stopped", batch_id)
                return
            # we are being shut down; hand the batch back so it can be resumed
            await self._repo.stop_batch(batch_id, BatchStatus.INTERRUPTED, "service shut down mid-batch")
            raise
        except Exception as exc:
            log.exception("batch %s failed unexpectedly", batch_id)
            await self._repo.stop_batch(batch_id, BatchStatus.INTERRUPTED, f"internal error: {exc!r}")
        finally:
            beat.cancel()

    async def _heartbeat(self, batch_id: UUID, work: asyncio.Task) -> None:
        while True:
            await asyncio.sleep(self._settings.heartbeat_interval_seconds)
            try:
                alive = await self._repo.heartbeat(batch_id)
            except Exception:
                # a missed beat is survivable; the sweeper only acts after several
                log.exception("heartbeat failed for batch %s", batch_id)
                continue
            if not alive:
                work.cancel()
                return

    async def _run(self, batch_id: UUID) -> None:
        batch = await self._repo.get_batch(batch_id)
        if batch is None or batch.status not in (BatchStatus.PROCESSING, BatchStatus.ACTIVATING):
            return

        try:
            await self._client.warm_up()
        except UpstreamUnavailable as exc:
            failed = BatchStatus.FAILED if batch.status is BatchStatus.PROCESSING else BatchStatus.ACTIVATION_FAILED
            await self._repo.stop_batch(batch_id, failed, str(exc))
            return

        if batch.status is BatchStatus.PROCESSING:
            await self._process_rows(batch_id)
            if not await self._repo.start_activation(batch_id):
                rows = await self._repo.get_rows(batch_id)
                not_created = sum(r.status is not RowStatus.CREATED for r in rows)
                await self._repo.stop_batch(
                    batch_id, BatchStatus.FAILED, f"{not_created} of {len(rows)} rows were not created; batch not activated"
                )
                return

        await self._activate(batch_id)

    async def _process_rows(self, batch_id: UUID) -> None:
        for _ in range(self._settings.max_passes):
            await self._reconcile(batch_id)
            pending = await self._repo.get_rows(batch_id, [RowStatus.PENDING])
            if not pending:
                return
            async with asyncio.TaskGroup() as group:
                for row in pending:
                    group.create_task(self._create_row(batch_id, row))
        # settle what the last pass left unknown, so the final report is as accurate as possible
        await self._reconcile(batch_id)

    async def _create_row(self, batch_id: UUID, row: RowRecord) -> None:
        while True:
            attempt = await self._repo.mark_in_flight(batch_id, row.row_no)
            if attempt is None:
                return

            outcome = await self._client.create_hospital(batch_id, row.name, row.address, row.phone)
            record = self._repo.record_row_outcome
            match outcome:
                case Created(hospital_id=hospital_id):
                    await record(batch_id, row.row_no, RowStatus.CREATED, hospital_id=hospital_id)
                    return
                case Rejected(status_code=code, detail=detail):
                    await record(batch_id, row.row_no, RowStatus.REJECTED, error=f"{code}: {detail}")
                    return
                case Unknown(reason=reason):
                    await record(batch_id, row.row_no, RowStatus.UNKNOWN, error=reason)
                    return
                case Retryable(reason=reason, retry_after=retry_after):
                    if attempt >= self._settings.max_attempts:
                        await record(batch_id, row.row_no, RowStatus.RETRY_EXHAUSTED, error=reason)
                        return
                    await record(batch_id, row.row_no, RowStatus.PENDING, error=reason)
                    # the client's semaphore is not held here, so a sleeping row blocks no one
                    await asyncio.sleep(self._backoff(attempt, retry_after))

    async def _reconcile(self, batch_id: UUID) -> None:
        rows = await self._repo.get_rows(batch_id)
        unknown = [r for r in rows if r.status is RowStatus.UNKNOWN]
        if not unknown:
            return

        # a request we gave up on may still be committing upstream; give it time before we look
        await asyncio.sleep(self._settings.reconcile_delay_seconds)
        try:
            upstream = await self._client.list_batch(batch_id)
        except UpstreamUnavailable:
            log.warning("could not reconcile batch %s; %d rows stay unknown", batch_id, len(unknown))
            return

        owned = {r.upstream_hospital_id for r in rows if r.upstream_hospital_id is not None}
        matches = match_unknown_rows(unknown, upstream, owned)
        await self._repo.apply_reconciliation(batch_id, matches, self._settings.max_attempts)

    async def _activate(self, batch_id: UUID) -> None:
        # PATCH activate is not idempotent upstream (a repeat returns 400 "already active"), so
        # after any ambiguous or rejected attempt we look at the batch instead of re-sending.
        error = "activation was not attempted"
        for attempt in range(1, self._settings.max_attempts + 1):
            outcome = await self._client.activate_batch(batch_id)
            if isinstance(outcome, Activated):
                await self._repo.complete_batch(batch_id)
                return

            if isinstance(outcome, Retryable):
                error = outcome.reason
                await asyncio.sleep(self._backoff(attempt, outcome.retry_after))
                continue

            error = f"{outcome.status_code}: {outcome.detail}" if isinstance(outcome, Rejected) else outcome.reason
            state = await self._activation_state(batch_id)
            if state is ActivationState.ACTIVE:
                await self._repo.complete_batch(batch_id)
                return
            if state is ActivationState.MIXED:
                error = "upstream batch is partly active; it cannot be activated as a whole"
                break
            if state is ActivationState.MISSING:
                error = "upstream no longer has hospitals created by this batch (it may have restarted)"
                break
            if state is ActivationState.INACTIVE and isinstance(outcome, Rejected):
                break  # nothing happened and the upstream refused; the same request won't succeed
            # still inactive after an ambiguous failure, or we couldn't check: safe to try again
            await asyncio.sleep(self._backoff(attempt))

        await self._repo.stop_batch(batch_id, BatchStatus.ACTIVATION_FAILED, error)

    async def _activation_state(self, batch_id: UUID) -> ActivationState | None:
        try:
            upstream = await self._client.list_batch(batch_id)
        except UpstreamUnavailable:
            return None
        rows = await self._repo.get_rows(batch_id)
        return activation_state({r.upstream_hospital_id for r in rows if r.upstream_hospital_id is not None}, upstream)

    def _backoff(self, attempt: int, retry_after: float | None = None) -> float:
        s = self._settings
        return backoff_delay(attempt, s.backoff_base_seconds, s.backoff_cap_seconds, retry_after, self._rand)


class RunnerTasks:
    """Keeps strong references to running batches (asyncio only holds weak ones) and stops them on shutdown."""

    def __init__(self, runner: Runner):
        self._runner = runner
        self._tasks: set[asyncio.Task] = set()

    def schedule(self, batch_id: UUID) -> None:
        task = asyncio.create_task(self._runner.run(batch_id), name=f"batch-{batch_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def shutdown(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
