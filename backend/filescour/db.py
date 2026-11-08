"""Database connection and schema set-up."""

from __future__ import annotations

import os
from importlib import resources

import psycopg

ENV_URL = "FILESCOUR_DATABASE_URL"


def database_url() -> str:
    url = os.environ.get(ENV_URL)
    if not url:
        raise RuntimeError(
            f"{ENV_URL} is not set. Example: "
            "postgresql://localhost/filescour"
        )
    return url


def connect(url: str | None = None) -> psycopg.Connection:
    """Open a connection. Each statement commits unless a transaction is opened."""
    # Times come back in UTC, whatever the server's own time zone is.
    return psycopg.connect(url or database_url(), autocommit=True,
                           options="-c TimeZone=UTC")


def init_schema(conn: psycopg.Connection) -> None:
    sql = resources.files("filescour").joinpath("schema.sql").read_text("utf-8")
    conn.execute(sql)
