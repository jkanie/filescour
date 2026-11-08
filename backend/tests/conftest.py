"""Shared test set-up.

The tests run against a real PostgreSQL. Point FILESCOUR_TEST_DATABASE_URL at
an empty database that the tests may wipe, for example:

    export FILESCOUR_TEST_DATABASE_URL=postgresql://localhost/filescour_test
"""

from __future__ import annotations

import os

import pytest

from filescour import db

TEST_URL_ENV = "FILESCOUR_TEST_DATABASE_URL"


@pytest.fixture(scope="session")
def test_url() -> str:
    url = os.environ.get(TEST_URL_ENV)
    if not url:
        pytest.fail(f"{TEST_URL_ENV} is not set, so there is no database to test against")
    os.environ[db.ENV_URL] = url
    with db.connect(url) as conn:
        db.init_schema(conn)
    return url


def wipe(conn) -> None:
    conn.execute("TRUNCATE roots, files, jobs, fetches RESTART IDENTITY CASCADE")


@pytest.fixture
def conn(test_url):
    with db.connect(test_url) as c:
        wipe(c)
        yield c
