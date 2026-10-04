from uuid import uuid4

from app.domain.models import BatchStatus, RowInput, RowStatus
from app.persistence.repository import ResumeClaim


async def run_batch(env, *names):
    batch_id = uuid4()
    await env.repo.create_batch(batch_id, [RowInput(i, name, "1 Main St", None) for i, name in enumerate(names, 1)])
    await env.runner.run(batch_id)
    return batch_id


async def state(env, batch_id):
    batch = await env.repo.get_batch(batch_id)
    rows = await env.repo.get_rows(batch_id)
    return batch, {r.name: r for r in rows}


async def test_happy_path_creates_every_row_then_activates(env):
    batch_id = await run_batch(env, "A", "B", "C")

    batch, rows = await state(env, batch_id)
    assert batch.status is BatchStatus.COMPLETED
    assert all(r.status is RowStatus.CREATED and r.upstream_hospital_id for r in rows.values())
    assert all(h["active"] for h in env.upstream.in_batch(batch_id))
    assert env.upstream.posts == {"A": 1, "B": 1, "C": 1}


async def test_timeout_after_commit_is_adopted_not_duplicated(env):
    env.upstream.post_script["B"] = ["commit_then_timeout"]

    batch_id = await run_batch(env, "A", "B")

    batch, rows = await state(env, batch_id)
    assert batch.status is BatchStatus.COMPLETED
    assert env.upstream.posts["B"] == 1  # reconciliation found it; no second POST
    assert len(env.upstream.in_batch(batch_id)) == 2
    assert rows["B"].upstream_hospital_id == next(h["id"] for h in env.upstream.in_batch(batch_id) if h["name"] == "B")


async def test_timeout_before_commit_is_verified_absent_then_resent(env):
    env.upstream.post_script["B"] = ["timeout_no_commit"]

    batch_id = await run_batch(env, "A", "B")

    batch, rows = await state(env, batch_id)
    assert batch.status is BatchStatus.COMPLETED
    assert env.upstream.posts["B"] == 2
    assert rows["B"].attempts == 2
    assert len(env.upstream.in_batch(batch_id)) == 2


async def test_identical_rows_with_an_ambiguous_failure_end_up_as_two_distinct_hospitals(env):
    env.upstream.post_script["Same"] = ["ok", "commit_then_timeout"]

    batch_id = await run_batch(env, "Same", "Same")

    batch, _ = await state(env, batch_id)
    rows = await env.repo.get_rows(batch_id)
    assert batch.status is BatchStatus.COMPLETED
    assert len({r.upstream_hospital_id for r in rows}) == 2
    assert len(env.upstream.in_batch(batch_id)) == 2


async def test_retryable_failures_are_retried_with_backoff(env):
    env.upstream.post_script["A"] = ["connect_error", "rate_limited"]

    batch_id = await run_batch(env, "A")

    batch, rows = await state(env, batch_id)
    assert batch.status is BatchStatus.COMPLETED
    assert rows["A"].attempts == 3


async def test_rejected_row_fails_the_batch_without_activating(env):
    env.upstream.post_script["B"] = ["reject"]

    batch_id = await run_batch(env, "A", "B")

    batch, rows = await state(env, batch_id)
    assert batch.status is BatchStatus.FAILED
    assert "1 of 2 rows were not created" in batch.last_error
    assert rows["B"].status is RowStatus.REJECTED
    assert rows["B"].last_error == "422: name is invalid"
    assert rows["A"].status is RowStatus.CREATED
    assert env.upstream.activations == 0
    assert await env.repo.claim_for_resume(batch_id) == (ResumeClaim.HAS_REJECTED_ROWS, BatchStatus.FAILED)


async def test_exhausted_row_fails_the_batch_and_resume_finishes_it_without_resending_created_rows(env):
    env.upstream.post_script["B"] = ["connect_error"] * 4

    batch_id = await run_batch(env, "A", "B")

    batch, rows = await state(env, batch_id)
    assert batch.status is BatchStatus.FAILED
    assert rows["B"].status is RowStatus.RETRY_EXHAUSTED

    assert (await env.repo.claim_for_resume(batch_id))[0] is ResumeClaim.CLAIMED
    await env.runner.run(batch_id)

    batch, rows = await state(env, batch_id)
    assert batch.status is BatchStatus.COMPLETED
    assert env.upstream.posts == {"A": 1, "B": 5}


async def test_interrupted_batch_resumes_and_reconciles_its_unknown_rows_first(env):
    batch_id = uuid4()
    await env.repo.create_batch(batch_id, [RowInput(1, "A", "1 Main St", None), RowInput(2, "B", "1 Main St", None)])
    # simulate a crash mid-POST: row A was sent and committed upstream, then the process died
    await env.repo.mark_in_flight(batch_id, 1)
    env.upstream._store({"name": "A", "address": "1 Main St", "phone": None, "creation_batch_id": str(batch_id)})
    await env.repo.stop_batch(batch_id, BatchStatus.INTERRUPTED, "runner stopped heartbeating")

    assert (await env.repo.claim_for_resume(batch_id))[0] is ResumeClaim.CLAIMED
    await env.runner.run(batch_id)

    batch, rows = await state(env, batch_id)
    assert batch.status is BatchStatus.COMPLETED
    assert env.upstream.posts == {"B": 1}  # A was adopted, never re-sent


async def test_activation_that_committed_but_timed_out_is_verified_not_repeated(env):
    env.upstream.activate_script = ["commit_then_timeout"]

    batch_id = await run_batch(env, "A")

    batch, _ = await state(env, batch_id)
    assert batch.status is BatchStatus.COMPLETED
    assert env.upstream.activations == 1


async def test_activation_server_error_is_checked_then_retried(env):
    env.upstream.activate_script = ["server_error"]

    batch_id = await run_batch(env, "A")

    batch, _ = await state(env, batch_id)
    assert batch.status is BatchStatus.COMPLETED
    assert env.upstream.activations == 2


async def test_partly_active_upstream_batch_fails_activation(env):
    env.upstream.activate_script = ["partial_then_server_error"]

    batch_id = await run_batch(env, "A", "B")

    batch, _ = await state(env, batch_id)
    assert batch.status is BatchStatus.ACTIVATION_FAILED
    assert "partly active" in batch.last_error
    assert env.upstream.activations == 1  # inspected, not blindly re-sent


async def test_upstream_that_never_wakes_fails_the_batch_before_any_write(env):
    env.upstream.warm_up_fails = True

    batch_id = await run_batch(env, "A")

    batch, rows = await state(env, batch_id)
    assert batch.status is BatchStatus.FAILED
    assert "warm-up failed" in batch.last_error
    assert rows["A"].status is RowStatus.PENDING
    assert sum(env.upstream.posts.values()) == 0
