"""Fixtures for the storage conformance suite.

Every connector listed in ``BACKENDS`` runs the same tests. A connector is
"supported" only when it passes all of them. Each backend entry builds a fresh,
empty database URL for one test.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from ai_meterix.storage import StorageReader, StorageWriter, open_reader, open_writer

BACKENDS: dict[str, Callable[[Path], str]] = {
    "sqlite": lambda tmp: f"sqlite:///{tmp / 'meter.db'}",
}


@pytest.fixture(params=sorted(BACKENDS))
def url(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    return BACKENDS[request.param](tmp_path)


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
