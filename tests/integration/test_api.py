"""End to end through HTTP: real app, real Postgres, upstream faked at the httpx transport."""

import asyncio

import httpx
import pytest

from app.main import create_app
from tests.integration.conftest import fast_settings

CSV = b"name,address,phone\nGeneral,1 Main St,555-1234\nCity,2 Oak Ave,\n"


@pytest.fixture
async def api(engine, upstream):
    app = create_app(fast_settings(max_upload_bytes=10_000))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield client


def upload(content: bytes, filename="hospitals.csv"):
    return {"files": {"file": (filename, content, "text/csv")}}


async def wait_until_settled(api, url, timeout=5.0) -> dict:
    async def poll():
        while True:
            body = (await api.get(url)).json()
            if body["status"] not in ("processing", "activating"):
                return body
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(poll(), timeout)


async def test_upload_is_accepted_then_completes_with_the_spec_response_shape(api, upstream):
    response = await api.post("/hospitals/bulk", **upload(CSV))

    assert response.status_code == 202
    accepted = response.json()
    assert response.headers["location"] == accepted["status_url"] == f"/hospitals/bulk/{accepted['batch_id']}"

    result = await wait_until_settled(api, accepted["status_url"])
    assert result["status"] == "completed"
    assert result["total_hospitals"] == 2
    assert result["processed_hospitals"] == 2
    assert result["failed_hospitals"] == 0
    assert result["batch_activated"] is True
    assert result["processing_time_seconds"] >= 0
    assert [(h["row"], h["name"], h["status"]) for h in result["hospitals"]] == [
        (1, "General", "created_and_activated"),
        (2, "City", "created_and_activated"),
    ]
    assert {h["hospital_id"] for h in result["hospitals"]} == {h["id"] for h in upstream.hospitals.values()}


async def test_invalid_csv_is_rejected_with_every_error_and_nothing_is_sent(api, upstream):
    response = await api.post("/hospitals/bulk", **upload(b"name,address,phone\n,1 Main St,\nCity,,\n"))

    assert response.status_code == 400
    assert response.json()["errors"] == [
        {"message": "must not be empty", "row": 1, "line": 2, "column": "name"},
        {"message": "must not be empty", "row": 2, "line": 3, "column": "address"},
    ]
    assert not upstream.posts


async def test_oversized_upload_is_rejected(api):
    response = await api.post("/hospitals/bulk", **upload(b"x" * 10_001))

    assert response.status_code == 413


async def test_oversized_request_is_rejected_before_parsing(api):
    response = await api.post("/hospitals/bulk", **upload(b"x" * 200_000))

    assert response.status_code == 413
    assert "request body exceeds" in response.json()["detail"]


async def test_validate_reports_without_creating_anything(api, upstream):
    response = await api.post("/hospitals/bulk/validate", **upload(CSV))

    assert response.json() == {"valid": True, "total_hospitals": 2, "errors": []}
    assert not upstream.posts


async def test_failed_batch_reports_row_errors_and_can_be_resumed(api, upstream):
    upstream.post_script["City"] = ["connect_error"] * 4
    accepted = (await api.post("/hospitals/bulk", **upload(CSV))).json()

    failed = await wait_until_settled(api, accepted["status_url"])
    assert failed["status"] == "failed"
    assert failed["batch_activated"] is False
    assert failed["failed_hospitals"] == 1
    assert failed["hospitals"][1]["status"] == "retry_exhausted"
    assert "ConnectError" in failed["hospitals"][1]["error"]

    resumed = await api.post(f"{accepted['status_url']}/resume")
    assert resumed.status_code == 202

    done = await wait_until_settled(api, accepted["status_url"])
    assert done["status"] == "completed"
    assert upstream.posts == {"General": 1, "City": 5}


async def test_resume_refuses_completed_and_unknown_batches(api):
    accepted = (await api.post("/hospitals/bulk", **upload(CSV))).json()
    await wait_until_settled(api, accepted["status_url"])

    completed = await api.post(f"{accepted['status_url']}/resume")
    assert completed.status_code == 409
    assert "completed" in completed.json()["detail"]

    missing = await api.post("/hospitals/bulk/00000000-0000-0000-0000-000000000000/resume")
    assert missing.status_code == 404


async def test_unknown_batch_is_404(api):
    assert (await api.get("/hospitals/bulk/00000000-0000-0000-0000-000000000000")).status_code == 404
