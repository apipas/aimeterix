"""Fixtures for the storage conformance suite.

Every connector listed in ``BACKENDS`` runs the same tests. A connector is
"supported" only when it passes all of them. Each backend entry builds a fresh,
empty database URL for one test.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest

from ai_meterix.storage import StorageReader, StorageWriter, open_reader, open_writer

# Postgres runs when this points at a server where the user may create databases,
# e.g. postgresql://postgres@127.0.0.1:5432/postgres (CI sets it).
POSTGRES_ENV = "AIMETERIX_TEST_POSTGRES_URL"


def _postgres_database(admin_url: str) -> Iterator[str]:
    import psycopg

    name = f"aimeterix_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {name}")
    try:
        yield urlunsplit(urlsplit(admin_url)._replace(path=f"/{name}"))
    finally:
        with psycopg.connect(admin_url, autocommit=True) as admin:
            admin.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")


BACKENDS = [
    pytest.param("sqlite", id="sqlite"),
    pytest.param(
        "duckdb",
        id="duckdb",
        marks=pytest.mark.skipif(
            importlib.util.find_spec("duckdb") is None, reason="duckdb extra not installed"
        ),
    ),
    pytest.param(
        "postgres",
        id="postgres",
        marks=pytest.mark.skipif(
            not os.environ.get(POSTGRES_ENV), reason=f"{POSTGRES_ENV} not set"
        ),
    ),
]


@pytest.fixture(params=BACKENDS)
def url(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    if request.param == "sqlite":
        yield f"sqlite:///{tmp_path / 'meter.db'}"
    elif request.param == "duckdb":
        yield f"duckdb:///{tmp_path / 'meter.duckdb'}"
    else:
        yield from _postgres_database(os.environ[POSTGRES_ENV])


@pytest.fixture
def writer(url: str) -> Iterator[StorageWriter]:
    storage = open_writer(url)
    storage.migrate()
    yield storage
    storage.close()


@pytest.fixture
def reader(url: str, writer: StorageWriter) -> Iterator[StorageReader]:
    storage = open_reader(url)
    yield storage
    storage.close()
