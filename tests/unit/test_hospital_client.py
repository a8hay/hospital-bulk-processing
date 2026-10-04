import asyncio
from uuid import uuid4

import httpx
import pytest
import respx

from app.integration.hospital_client import (
    Activated,
    Created,
    HospitalClient,
    Rejected,
    Retryable,
    Unknown,
    UpstreamHospital,
    UpstreamUnavailable,
)

BASE = "https://upstream.test"
BATCH_ID = uuid4()


@pytest.fixture
async def client():
    async with httpx.AsyncClient(base_url=BASE) as http:
        yield HospitalClient(http, concurrency=5)


@pytest.fixture
def upstream():
    with respx.mock(base_url=BASE, assert_all_called=False) as mock:
        yield mock


async def create(client):
    return await client.create_hospital(BATCH_ID, "General", "1 Main St", None)


class TestCreateHospital:
    async def test_sends_batch_id_and_returns_created(self, client, upstream):
        route = upstream.post("/hospitals/").respond(200, json={"id": 101, "active": False})

        assert await create(client) == Created(101)
        assert route.calls.last.request.read() == (
            f'{{"name":"General","address":"1 Main St","phone":null,"creation_batch_id":"{BATCH_ID}"}}'.encode()
        )

    @pytest.mark.parametrize(
        "status, body",
        [
            (422, {"detail": [{"msg": "String should have at least 1 character"}]}),
            (400, {"detail": "Batch cannot exceed 20 hospitals"}),
        ],
    )
    async def test_client_errors_are_rejected_with_detail(self, client, upstream, status, body):
        upstream.post("/hospitals/").respond(status, json=body)

        assert await create(client) == Rejected(status, str(body["detail"]))

    async def test_rate_limit_is_retryable_and_honours_retry_after(self, client, upstream):
        upstream.post("/hospitals/").respond(429, headers={"Retry-After": "7"}, json={"error": "limited"})

        assert await create(client) == Retryable("rate limited", 7.0)

    async def test_rate_limit_without_retry_after(self, client, upstream):
        upstream.post("/hospitals/").respond(429, json={"error": "Rate limit exceeded: 30 per 1 minute"})

        assert await create(client) == Retryable("rate limited", None)

    @pytest.mark.parametrize("status", [500, 502, 503])
    async def test_server_errors_are_unknown(self, client, upstream, status):
        upstream.post("/hospitals/").respond(status)

        assert isinstance(await create(client), Unknown)

    @pytest.mark.parametrize(
        "error", [httpx.ConnectError("refused"), httpx.ConnectTimeout("slow"), httpx.PoolTimeout("busy")]
    )
    async def test_errors_before_sending_are_retryable(self, client, upstream, error):
        upstream.post("/hospitals/").mock(side_effect=error)

        assert isinstance(await create(client), Retryable)

    @pytest.mark.parametrize(
        "error",
        [httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("dropped"), httpx.ReadError("reset")],
    )
    async def test_errors_after_sending_are_unknown(self, client, upstream, error):
        upstream.post("/hospitals/").mock(side_effect=error)

        assert isinstance(await create(client), Unknown)

    async def test_success_without_readable_id_is_unknown(self, client, upstream):
        upstream.post("/hospitals/").respond(200, text="not json")

        assert isinstance(await create(client), Unknown)


class TestActivateBatch:
    async def test_success(self, client, upstream):
        upstream.patch(f"/hospitals/batch/{BATCH_ID}/activate").respond(200, json={})

        assert await client.activate_batch(BATCH_ID) == Activated()

    async def test_already_active_is_rejected_for_the_runner_to_verify(self, client, upstream):
        detail = "Cannot activate batch: one or more hospitals in the batch are already active"
        upstream.patch(f"/hospitals/batch/{BATCH_ID}/activate").respond(400, json={"detail": detail})

        assert await client.activate_batch(BATCH_ID) == Rejected(400, detail)

    async def test_read_timeout_is_unknown(self, client, upstream):
        upstream.patch(f"/hospitals/batch/{BATCH_ID}/activate").mock(side_effect=httpx.ReadTimeout("slow"))

        assert isinstance(await client.activate_batch(BATCH_ID), Unknown)


class TestListBatch:
    async def test_parses_hospitals(self, client, upstream):
        upstream.get(f"/hospitals/batch/{BATCH_ID}").respond(
            200,
            json=[
                {"id": 1, "name": "A", "address": "B", "phone": None, "active": False, "creation_batch_id": "x"},
            ],
        )

        assert await client.list_batch(BATCH_ID) == [UpstreamHospital(1, "A", "B", None, False)]

    async def test_404_means_no_hospitals(self, client, upstream):
        upstream.get(f"/hospitals/batch/{BATCH_ID}").respond(
            404, json={"detail": "No hospitals found with the specified batch ID"}
        )

        assert await client.list_batch(BATCH_ID) == []

    @pytest.mark.parametrize("mock", [{"side_effect": httpx.ReadTimeout("slow")}, {"return_value": httpx.Response(503)}])
    async def test_failures_raise_unavailable(self, client, upstream, mock):
        upstream.get(f"/hospitals/batch/{BATCH_ID}").mock(**mock)

        with pytest.raises(UpstreamUnavailable):
            await client.list_batch(BATCH_ID)


class TestWarmUp:
    async def test_success(self, client, upstream):
        upstream.get("/").respond(200)

        await client.warm_up()

    async def test_failure_raises_unavailable(self, client, upstream):
        upstream.get("/").mock(side_effect=httpx.ConnectError("refused"))

        with pytest.raises(UpstreamUnavailable):
            await client.warm_up()


async def test_concurrency_is_capped_across_all_calls():
    in_flight = peak = 0

    async def handler(request):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        if request.method == "POST":
            return httpx.Response(200, json={"id": 1})
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(handler)) as http:
        client = HospitalClient(http, concurrency=3)
        await asyncio.gather(
            *[create(client) for _ in range(10)],
            *[client.list_batch(BATCH_ID) for _ in range(10)],
        )

    assert peak == 3
