from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # required, with no default: a missing DATABASE_URL must stop startup with a clear error,
    # not fall back to a local address and fail later with a confusing connection error
    database_url: str

    upstream_base_url: str = "https://hospital-directory.onrender.com"
    upstream_concurrency: int = 10
    upstream_connect_timeout: float = 5.0
    upstream_read_timeout: float = 30.0  # measured max 7.45s warm; a false timeout costs a reconcile
    upstream_warm_up_timeout: float = 60.0  # measured cold start 27.6s

    max_rows: int = 20
    max_upload_bytes: int = 1_000_000

    max_attempts: int = 4  # POSTs per row per run
    max_passes: int = 3  # create/reconcile cycles per run
    backoff_base_seconds: float = 1.0
    backoff_cap_seconds: float = 8.0
    reconcile_delay_seconds: float = 5.0  # time for an in-flight upstream write to commit before we look

    heartbeat_interval_seconds: float = 5.0
    stale_after_seconds: float = 30.0
    sweep_interval_seconds: float = 30.0

    @field_validator("database_url")
    @classmethod
    def use_asyncpg_driver(cls, url: str) -> str:
        # Render and most hosts hand out postgres:// URLs; SQLAlchemy needs the driver named
        for prefix in ("postgres://", "postgresql://"):
            if url.startswith(prefix):
                return "postgresql+asyncpg://" + url.removeprefix(prefix)
        return url
