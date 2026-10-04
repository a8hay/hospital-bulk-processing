import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import create_async_engine

from app.api.routes import router
from app.config import Settings
from app.integration.hospital_client import HospitalClient, create_http_client
from app.persistence.repository import Repository, apply_schema
from app.runner import Runner, RunnerTasks
from app.sweeper import run_sweeper

MULTIPART_OVERHEAD_BYTES = 64_000


class MaxBodySizeMiddleware:
    """Reject oversized uploads from Content-Length before the multipart parser reads the body.

    A client using chunked encoding sends no Content-Length; the bounded read in the route still
    caps what we keep in memory, but the parser will have spooled the body to disk by then.
    """

    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            length = dict(scope["headers"]).get(b"content-length", b"")
            if length.isdigit() and int(length) > self.max_bytes:
                response = JSONResponse({"detail": f"request body exceeds {self.max_bytes} bytes"}, status_code=413)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        await apply_schema(engine)
        http = create_http_client(
            settings.upstream_base_url,
            settings.upstream_concurrency,
            settings.upstream_connect_timeout,
            settings.upstream_read_timeout,
        )
        client = HospitalClient(http, settings.upstream_concurrency, settings.upstream_warm_up_timeout)
        repo = Repository(engine)
        tasks = RunnerTasks(Runner(repo, client, settings))
        sweeper = asyncio.create_task(
            run_sweeper(repo, settings.sweep_interval_seconds, settings.stale_after_seconds), name="sweeper"
        )

        app.state.settings = settings
        app.state.repo = repo
        app.state.tasks = tasks
        try:
            yield
        finally:
            # await the cancelled sweeper: disposing the engine while it is mid-transaction would
            # leave that transaction open in Postgres, holding its row locks
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
            await tasks.shutdown()
            await http.aclose()
            await engine.dispose()

    app = FastAPI(
        title="Hospital Bulk Processing",
        description="Bulk CSV upload in front of the Hospital Directory API.",
        lifespan=lifespan,
    )
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES)
    app.include_router(router)

    @app.get("/health", tags=["ops"])
    async def health() -> dict:
        return {"status": "ok"}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
