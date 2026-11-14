"""The job queue, kept in PostgreSQL.

Several workers can run at once. Each one claims the oldest queued job with
FOR UPDATE SKIP LOCKED, so 2 workers never take the same job, and neither one
waits on a row that the other has locked.

Just before a worker opens a file, it reads the file's cloud state again
(without opening it). A file that went online-only after the scan is skipped,
not read, so the worker never makes a cloud service down-load it. A zip
file gets the same check before its entries are listed (see archive.py).

A worker that dies mid-job leaves its job in 'running'. Each run of the queue
first puts every job that has been running for longer than STUCK_AFTER back in
the queue, with the reason kept in its error column. A job that has already
been claimed MAX_ATTEMPTS times fails for good instead.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import psycopg

from . import archive, cloud
from .extract import ZIP_EXTS, ExtractError, extract

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


STUCK_AFTER = timedelta(minutes=30)
MAX_ATTEMPTS = 3

REQUEUE_SQL = """
UPDATE jobs SET
    state = CASE WHEN attempts >= %(max)s::integer THEN 'failed' ELSE 'queued' END,
    error = CASE WHEN attempts >= %(max)s::integer
        THEN format('gave up after %%s attempts: running for more than %%s minutes each time',
                    attempts, %(minutes)s::text)
        ELSE format('re-queued after attempt %%s: running for more than %%s minutes',
                    attempts, %(minutes)s::text)
    END,
    updated_at = now()
WHERE state = 'running' AND updated_at < now() - %(after)s::interval
RETURNING state
"""


def requeue_stuck(conn: psycopg.Connection,
                  after: timedelta = STUCK_AFTER) -> tuple[int, int]:
    """Put jobs left in 'running' for longer than `after` back in the queue.

    A job claimed MAX_ATTEMPTS times already is failed instead. Returns
    (re-queued, failed).
    """
    minutes = after.total_seconds() / 60
    with conn.transaction():
        rows = conn.execute(REQUEUE_SQL, {
            "max": MAX_ATTEMPTS, "after": after, "minutes": f"{minutes:g}",
        }).fetchall()
    failed = sum(1 for (state,) in rows if state == "failed")
    return len(rows) - failed, failed


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
    """Extract the text of 1 file, or the entries of 1 zip file.

    Returns the job's final state.
    """
    row = conn.execute(
        "SELECT f.path, f.ext, f.mtime, r.extract_unknown FROM files f "
        "JOIN roots r ON r.id = f.root_id WHERE f.id = %s", (file_id,)
    ).fetchone()
    if row is None:  # the file row was removed after the job was claimed
        _finish(conn, job_id, "failed", "file row no longer exists")
        return "failed"
    path, ext, mtime, extract_unknown = row
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
    if ext in ZIP_EXTS:
        return _expand(conn, job_id, file_id, path, mtime, state, attrs)
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


def _expand(conn, job_id: int, file_id: int, path: str, mtime, state: str,
            attrs) -> str:
    """List the entries of a zip file. Its old entries go in the same step."""
    try:
        with conn.transaction():
            archive.expand(conn, file_id, path, mtime, state)
            conn.execute(
                "UPDATE files SET cloud = %s, attrs = %s, indexed_at = now() "
                "WHERE id = %s", (state, attrs, file_id),
            )
            _finish(conn, job_id, "done", None)
        return "done"
    except (archive.ArchiveError, OSError) as exc:
        error = str(exc)
    with conn.transaction():
        conn.execute("DELETE FROM files WHERE parent_id = %s", (file_id,))
        conn.execute("UPDATE files SET indexed_at = now() WHERE id = %s", (file_id,))
        _finish(conn, job_id, "failed", error)
    return "failed"


def run(conn: psycopg.Connection, limit: int | None = None,
        stuck_after: timedelta = STUCK_AFTER) -> int:
    """Work through queued jobs until none is left (or limit is reached).

    Stuck jobs go back in the queue first (see requeue_stuck). Returns the
    count of jobs run.
    """
    return run_counts(conn, limit, stuck_after)["processed"]


def run_counts(conn: psycopg.Connection, limit: int | None = None,
               stuck_after: timedelta = STUCK_AFTER) -> dict[str, int]:
    """Like run, but also says how many stuck jobs went back in the queue
    (requeued) and how many failed for good after MAX_ATTEMPTS (gave_up)."""
    requeued, gave_up = requeue_stuck(conn, stuck_after)
    return {"processed": _work(conn, limit), "requeued": requeued, "gave_up": gave_up}


def _work(conn: psycopg.Connection, limit: int | None) -> int:
    count = 0
    while limit is None or count < limit:
        job = claim(conn)
        if job is None:
            break
        process(conn, *job)
        count += 1
    return count


def summary(conn: psycopg.Connection, root_id: int | None = None) -> dict[str, int]:
    """Counts jobs by state, for every folder or, with root_id, for the files of 1 folder."""
    counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "skipped": 0}
    if root_id is None:
        rows = conn.execute("SELECT state, count(*) FROM jobs GROUP BY state")
    else:
        rows = conn.execute(
            "SELECT j.state, count(*) FROM jobs j JOIN files f ON f.id = j.file_id"
            " WHERE f.root_id = %s GROUP BY j.state",
            (root_id,),
        )
    for state, n in rows:
        counts[state] = n
    return counts
