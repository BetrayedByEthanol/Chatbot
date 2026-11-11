"""Integration suite applies ordered SQL migrations to a disposable test DB."""

import os
from pathlib import Path

import pytest

@pytest.fixture(scope="session")
def dsn():
    value = os.environ.get("TEST_DATABASE_URL")
    if not value:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    psycopg = pytest.importorskip("psycopg")
    migrations = sorted((Path(__file__).resolve().parents[1] / "migrations").glob("*.sql"))
    with psycopg.connect(value, autocommit=True) as connection:
        for migration in migrations:
            with connection.transaction():
                connection.execute(migration.read_text())
    return value
