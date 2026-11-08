"""The job queue, kept in PostgreSQL.

Several workers can run at once. Each one claims the oldest queued job with
FOR UPDATE SKIP LOCKED, so 2 workers never take the same job, and neither one
waits on a row that the other has locked.

Just before a worker opens a file, it reads the file's cloud state again
(without opening it). A file that went online-only after the scan is skipped,
not read, so the worker never makes a cloud service down-load it.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from . import cloud
from .extract import ExtractError, extract

CLAIM_SQL = """
WITH next AS (
    SELECT id FROM jobs
    WHERE state = 'queued'
    ORDER BY id
    LIMIT 1
    FOR UPDATE SKIP LOCKED
)
UPDATE jobs SET state = 'running', attempts = attempts + 1, updated_at = now()
FROM next
WHERE jobs.id = next.id
RETURNING jobs.id, jobs.file_id
"""


def claim(conn: psycopg.Connection) -> tuple[int, int] | None:
    """Mark the oldest queued job as running and return (job id, file id)."""
    with conn.transaction():
        row = conn.execute(CLAIM_SQL).fetchone()
    return (row[0], row[1]) if row else None


def _finish(conn, job_id: int, state: str, error: str | None) -> None:
    conn.execute(
        "UPDATE jobs SET state = %s, error = %s, updated_at = now() WHERE id = %s",
        (state, error, job_id),
    )


def process(conn: psycopg.Connection, job_id: int, file_id: int) -> str:
    """Extract the text of 1 file. Returns the job's final state."""
    row = conn.execute(
        "SELECT f.path, f.ext, r.extract_unknown FROM files f "
        "JOIN roots r ON r.id = f.root_id WHERE f.id = %s", (file_id,)
    ).fetchone()
    if row is None:  # the file row was removed after the job was claimed
        _finish(conn, job_id, "failed", "file row no longer exists")
        return "failed"
    path, ext, extract_unknown = row
    state, attrs = cloud.current_state(path)
    if not cloud.may_read(state, extract_unknown):
        why = ("online only" if state == "online"
               else "Windows attributes cannot be read here")
        with conn.transaction():
            conn.execute(
                "UPDATE files SET cloud = %s, attrs = %s WHERE id = %s",
                (state, attrs, file_id),
            )
            _finish(conn, job_id, "skipped", f"not opened: {why}")
        return "skipped"
    try:
        got = extract(Path(path), ext)
    except (ExtractError, OSError) as exc:
        with conn.transaction():
            conn.execute(
                "UPDATE files SET indexed_at = now() WHERE id = %s", (file_id,)
            )
            _finish(conn, job_id, "failed", str(exc))
        return "failed"
    with conn.transaction():
        conn.execute(
            "UPDATE files SET content = %s, content_source = %s, truncated = %s, "
            "cloud = %s, attrs = %s, indexed_at = now() WHERE id = %s",
            (got.text, got.source, got.truncated, state, attrs, file_id),
        )
        _finish(conn, job_id, "done", None)
    return "done"


def run(conn: psycopg.Connection, limit: int | None = None) -> int:
    """Work through queued jobs until none is left (or limit is reached)."""
    count = 0
    while limit is None or count < limit:
        job = claim(conn)
        if job is None:
            break
        process(conn, *job)
        count += 1
    return count


def summary(conn: psycopg.Connection) -> dict[str, int]:
    counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "skipped": 0}
    for state, n in conn.execute("SELECT state, count(*) FROM jobs GROUP BY state"):
        counts[state] = n
    return counts
