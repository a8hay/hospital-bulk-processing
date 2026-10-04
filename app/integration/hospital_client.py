"""Thin, stateless wrapper over the Hospital Directory API.

Each method makes exactly one HTTP exchange and classifies the result. Retrying, backoff and
reconciliation are the runner's job: they need batch state this module deliberately knows
nothing about.

Classification follows one rule: if we cannot prove the upstream did NOT apply the request,
the outcome is Unknown, never Retryable. Retrying an Unknown blindly can create a duplicate.
"""

import asyncio
from dataclasses import dataclass
from uuid import UUID

import httpx


@dataclass(frozen=True)
class Created:
    hospital_id: int


@dataclass(frozen=True)
class Activated:
    pass


@dataclass(frozen=True)
class Rejected:
    """Upstream refused the request (4xx other than 429). Retrying the same request will not help."""

    status_code: int
    detail: str


@dataclass(frozen=True)
class Retryable:
    """The request provably did not take effect (never connected, or rate limited)."""

    reason: str
    retry_after: float | None = None


@dataclass(frozen=True)
class Unknown:
    """The request may or may not have taken effect upstream. Reconcile before retrying."""

    reason: str


CreateOutcome = Created | Rejected | Retryable | Unknown
ActivateOutcome = Activated | Rejected | Retryable | Unknown


@dataclass(frozen=True)
class UpstreamHospital:
    id: int
    name: str
    address: str
    phone: str | None
    active: bool


class UpstreamUnavailable(Exception):
    """A read-only call (warm-up, batch listing) failed; nothing upstream was changed."""


def create_http_client(base_url: str, concurrency: int, connect_timeout: float, read_timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=base_url,
        timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=connect_timeout, pool=read_timeout),
        # never fewer pooled connections than semaphore permits, or permit holders queue on the pool
        limits=httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency),
    )


def _detail(response: httpx.Response) -> str:
    try:
        return str(response.json().get("detail", response.text))
    except ValueError:
        return response.text


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["retry-after"])
    except (KeyError, ValueError):
        return None


def _classify_failure(response: httpx.Response) -> Rejected | Retryable | Unknown:
    if response.status_code == 429:
        return Retryable("rate limited", _retry_after(response))
    if 400 <= response.status_code < 500:
        return Rejected(response.status_code, _detail(response))
    # a 5xx may be raised after the write was committed
    return Unknown(f"upstream returned {response.status_code}")


def _classify_transport_error(exc: httpx.TransportError) -> Retryable | Unknown:
    # these fail before any byte of the request reaches the upstream
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return Retryable(f"{type(exc).__name__}: {exc}")
    # read timeouts, dropped connections, protocol errors: the request may have been processed
    return Unknown(f"{type(exc).__name__}: {exc}")


class HospitalClient:
    def __init__(self, http: httpx.AsyncClient, concurrency: int, warm_up_timeout: float = 60.0):
        self._http = http
        # the upstream rate-limits per server, so every call counts against one shared cap
        self._limit = asyncio.Semaphore(concurrency)
        self._warm_up_timeout = warm_up_timeout

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        async with self._limit:
            return await self._http.request(method, url, **kwargs)

    async def warm_up(self) -> None:
        """Wake the upstream from a cold start before any write is attempted."""
        try:
            response = await self._send("GET", "/", timeout=self._warm_up_timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"warm-up failed: {exc}") from exc

    async def create_hospital(
        self, batch_id: UUID, name: str, address: str, phone: str | None
    ) -> CreateOutcome:
        payload = {"name": name, "address": address, "phone": phone, "creation_batch_id": str(batch_id)}
        try:
            response = await self._send("POST", "/hospitals/", json=payload)
        except httpx.TransportError as exc:
            return _classify_transport_error(exc)

        if not response.is_success:
            return _classify_failure(response)
        try:
            return Created(int(response.json()["id"]))
        except (ValueError, KeyError, TypeError):
            # it was created, but we can't read the id; reconciliation will find it
            return Unknown(f"unreadable success response: {response.text[:200]}")

    async def activate_batch(self, batch_id: UUID) -> ActivateOutcome:
        try:
            response = await self._send("PATCH", f"/hospitals/batch/{batch_id}/activate")
        except httpx.TransportError as exc:
            return _classify_transport_error(exc)

        if response.is_success:
            return Activated()
        return _classify_failure(response)

    async def list_batch(self, batch_id: UUID) -> list[UpstreamHospital]:
        try:
            response = await self._send("GET", f"/hospitals/batch/{batch_id}")
        except httpx.TransportError as exc:
            raise UpstreamUnavailable(f"listing batch failed: {exc}") from exc

        # upstream answers 404 for a batch with no hospitals, which for us just means none exist yet
        if response.status_code == 404:
            return []
        if not response.is_success:
            raise UpstreamUnavailable(f"listing batch returned {response.status_code}: {_detail(response)}")

        return [
            UpstreamHospital(
                id=h["id"], name=h["name"], address=h["address"], phone=h.get("phone"), active=h["active"]
            )
            for h in response.json()
        ]
