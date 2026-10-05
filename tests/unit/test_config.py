import pytest
from pydantic import ValidationError

from app.config import Settings


def test_missing_database_url_fails_with_a_clear_error(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)


@pytest.mark.parametrize(
    "url",
    ["postgres://u:p@host:5432/db", "postgresql://u:p@host:5432/db", "postgresql+asyncpg://u:p@host:5432/db"],
)
def test_database_url_is_normalised_to_the_asyncpg_driver(monkeypatch, url):
    monkeypatch.setenv("DATABASE_URL", url)

    assert Settings(_env_file=None).database_url == "postgresql+asyncpg://u:p@host:5432/db"
