"""Finds batches whose runner died (crash, OOM, redeploy) and makes them resumable.

Without it, a dead runner's batch would say 'processing' forever and resume would refuse it.
"""

import asyncio
import logging

from app.persistence.repository import Repository

log = logging.getLogger(__name__)


async def run_sweeper(repo: Repository, interval_seconds: float, stale_after_seconds: float) -> None:
    while True:
        try:
            swept = await repo.sweep_stale(stale_after_seconds)
            if swept:
                log.warning("marked %d stale batches interrupted: %s", len(swept), swept)
        except Exception:
            log.exception("sweep failed")
        await asyncio.sleep(interval_seconds)
