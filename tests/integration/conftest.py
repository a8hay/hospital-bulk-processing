import json
import os
from collections import Counter, defaultdict
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import Settings
from app.integration.hospital_client import HospitalClient
from app.persistence.repository import Repository, apply_schema
from app.runner import Runner

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5434/hospital_bulk_test"
)
UPSTREAM = "https://upstream.test"


def fast_settings(**overrides) -> Settings:
    values = dict(
        database_url=TEST_DATABASE_URL,
        upstream_base_url=UPSTREAM,
        backoff_base_seconds=0.0,
        reconcile_delay_seconds=0.0,
        heartbeat_interval_seconds=0.05,
        sweep_interval_seconds=0.05,
    )
    return Settings(**(values | overrides))


@pytest.fixture
async def engine():
    # fail fast instead of hanging if a leaked transaction from an earlier test still holds locks
    engine = create_async_engine(TEST_DATABASE_URL, connect_args={"server_settings": {"lock_timeout": "5s"}})
    try:
        await apply_schema(engine)
    except OSError as exc:
        await engine.dispose()
        pytest.skip(f"Postgres not reachable at {TEST_DATABASE_URL} ({exc}); run `docker compose up -d db`")
    async with engine.begin() as conn:
        await conn.exec_driver_sql("TRUNCATE batch_job CASCADE")
    yield engine
    await engine.dispose()


@pytest.fixture
def repo(engine) -> Repository:
    return Repository(engine)


class FakeUpstream:
    """A tiny stateful stand-in for the Hospital Directory API, with scripted failures.

    It keeps just enough state (hospitals, ids, active flags) to show whether a failure
    committed or not, which is the whole point of reconciliation.
    """

    def __init__(self):
        self.hospitals: dict[int, dict] = {}
        self._next_id = 100
        self.post_script: dict[str, list[str]] = defaultdict(list)  # hospital name -> behaviour per POST
        self.activate_script: list[str] = []
        self.warm_up_fails = False
        self.posts = Counter()  # hospital name -> POSTs received
        self.activations = 0

    def install(self, router: respx.MockRouter) -> None:
        router.get("/").mock(side_effect=self._warm_up)
        router.post("/hospitals/").mock(side_effect=self._post)
        router.get(path__regex=r"^/hospitals/batch/(?P<batch_id>[^/]+)$").mock(side_effect=self._list)
        router.patch(path__regex=r"^/hospitals/batch/(?P<batch_id>[^/]+)/activate$").mock(side_effect=self._activate)

    def _warm_up(self, request):
        if self.warm_up_fails:
            raise httpx.ConnectError("upstream asleep")
        return httpx.Response(200, json={"message": "ok"})

    def in_batch(self, batch_id) -> list[dict]:
        return [h for h in self.hospitals.values() if h["creation_batch_id"] == str(batch_id)]

    def _store(self, body: dict) -> dict:
        self._next_id += 1
        hospital = {**body, "id": self._next_id, "active": False}
        self.hospitals[self._next_id] = hospital
        return hospital

    def _post(self, request):
        body = json.loads(request.content)
        self.posts[body["name"]] += 1
        script = self.post_script[body["name"]]
        behaviour = script.pop(0) if script else "ok"
        match behaviour:
            case "ok":
                return httpx.Response(200, json=self._store(body))
            case "commit_then_timeout":
                self._store(body)
                raise httpx.ReadTimeout("response lost after commit")
            case "timeout_no_commit":
                raise httpx.ReadTimeout("request lost before commit")
            case "connect_error":
                raise httpx.ConnectError("connection refused")
            case "rate_limited":
                return httpx.Response(429, json={"error": "Rate limit exceeded: 30 per 1 minute"})
            case "reject":
                return httpx.Response(422, json={"detail": "name is invalid"})
        raise AssertionError(f"unknown behaviour {behaviour}")

    def _list(self, request, batch_id):
        hospitals = self.in_batch(batch_id)
        if not hospitals:
            return httpx.Response(404, json={"detail": "No hospitals found with the specified batch ID"})
        return httpx.Response(200, json=hospitals)

    def _activate(self, request, batch_id):
        self.activations += 1
        behaviour = self.activate_script.pop(0) if self.activate_script else "ok"
        hospitals = self.in_batch(batch_id)
        if behaviour == "server_error":
            return httpx.Response(500)
        if behaviour == "partial_then_server_error":
            hospitals[0]["active"] = True
            return httpx.Response(500)
        if not hospitals:
            return httpx.Response(404, json={"detail": "No hospitals found with the specified batch ID"})
        if any(h["active"] for h in hospitals):
            return httpx.Response(
                400, json={"detail": "Cannot activate batch: one or more hospitals in the batch are already active"}
            )
        for h in hospitals:
            h["active"] = True
        if behaviour == "commit_then_timeout":
            raise httpx.ReadTimeout("response lost after commit")
        return httpx.Response(200, json={"activated_count": len(hospitals)})


@pytest.fixture
async def upstream():
    fake = FakeUpstream()
    with respx.mock(base_url=UPSTREAM, assert_all_called=False) as router:
        fake.install(router)
        yield fake


@pytest.fixture
async def runner(repo, upstream):
    async with httpx.AsyncClient(base_url=UPSTREAM) as http:
        yield Runner(repo, HospitalClient(http, concurrency=5), fast_settings(), rand=lambda: 0.0)


@pytest.fixture
def env(repo, upstream, runner) -> SimpleNamespace:
    return SimpleNamespace(repo=repo, upstream=upstream, runner=runner)
