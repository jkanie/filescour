"""Down-load online-only files to read them, then put them back.

A search can only look inside files whose text was read, and an online-only
file is never opened by a scan (see cloud.py). This module reads such files
on request, in 1 of 2 modes:

- temporary: down-load the file, read its text, then set it back to online
  only. The text stays in the index, and the disk space is given back.
- keep: down-load the file, read its text, and leave it "always available"
  on this device.

A request only adds rows to the journal (the fetches table). Running the
journal does the work, now or later. Each row records the file's attributes
before anything changes them. A row in mode temporary closes only once the
file is confirmed online only again, so after a crash or a restart, the next
run finishes putting back every file that was left down-loaded.

The changes use the OneDrive commands that Microsoft documents for Files
On-Demand states: "attrib +p" (always available, which down-loads the file),
"attrib -p" (clear the pin), "attrib +u" (online only, which frees the
space). See https://learn.microsoft.com/en-us/onedrive/files-on-demand-windows
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

import psycopg

from . import cloud
from .extract import ExtractError, extract, wants_content

MODES = ("temporary", "keep")
OPEN_STATES = ("queued", "downloading", "restoring")

MB = 1024**2

# The 3 limits of a run. Each can be set in the environment, and each run can
# override them (API parameters, command-line options, the screen's fields).
FREE_FLOOR_ENV = "FILESCOUR_FETCH_FREE_FLOOR_MB"  # free space to leave on the drive
MAX_FILE_ENV = "FILESCOUR_FETCH_MAX_FILE_MB"  # skip files larger than this
WAIT_ENV = "FILESCOUR_FETCH_WAIT_SECONDS"  # how long 1 file may take to change state
DEFAULTS = {"free_floor_mb": 2048, "max_file_mb": 500, "wait_seconds": 120}
ENVS = {"free_floor_mb": FREE_FLOOR_ENV, "max_file_mb": MAX_FILE_ENV,
        "wait_seconds": WAIT_ENV}
LOCK_KEY = 7_406_313_001  # 1 journal run at a time, across all processes


@dataclass(frozen=True)
class Limits:
    free_floor_mb: int
    max_file_mb: int
    wait_seconds: int

    @property
    def free_floor(self) -> int:
        return self.free_floor_mb * MB

    @property
    def max_size(self) -> int:
        return self.max_file_mb * MB


def limits(**overrides: int | None) -> Limits:
    """The limits for a run: an override, else the environment, else the default.

    Every value is a whole number, 0 or more. A bad value in the environment
    stops the run with an error rather than falling back silently.
    """
    out = {}
    for key, default in DEFAULTS.items():
        value = overrides.get(key)
        if value is None:
            raw = os.environ.get(ENVS[key], "").strip()
            if raw:
                try:
                    value = int(raw)
                except ValueError:
                    raise ValueError(f"{ENVS[key]} must be a whole number, not {raw!r}") from None
            else:
                value = default
        if value < 0:
            raise ValueError(f"{key} must be 0 or more")
        out[key] = value
    return Limits(**out)


class FetchUnavailable(RuntimeError):
    """This machine cannot down-load online-only files (not Windows)."""


class Ops(Protocol):
    """What the journal needs from the operating system."""

    def attributes(self, path: str) -> int | None: ...
    def pin(self, path: str) -> None: ...
    def unpin(self, path: str) -> None: ...
    def free_bytes(self, path: str) -> int: ...
    def sleep(self, seconds: float) -> None: ...


class WindowsOps:
    """The real thing. It runs only on Windows."""

    def attributes(self, path: str) -> int | None:
        return cloud._find_attributes(path)

    def pin(self, path: str) -> None:
        subprocess.run(["attrib", "+p", path], check=True, capture_output=True)

    def unpin(self, path: str) -> None:
        subprocess.run(["attrib", "-p", path], check=True, capture_output=True)
        subprocess.run(["attrib", "+u", path], check=True, capture_output=True)

    def free_bytes(self, path: str) -> int:
        return shutil.disk_usage(os.path.dirname(path) or ".").free

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def default_ops() -> Ops:
    if os.name != "nt":
        raise FetchUnavailable(
            "down-loading online-only files works only when filescour runs on Windows"
        )
    return WindowsOps()


@dataclass(frozen=True)
class Requested:
    queued: int
    bytes: int
    not_online: int  # on this disk already, or not known: nothing to down-load
    no_text: int  # a file type whose text is never read


@dataclass(frozen=True)
class RunResult:
    done: int
    skipped: int
    waiting: int  # rows still open: a file not yet confirmed back in place
    stopped: str | None  # why the run stopped early, if it did


def request(conn: psycopg.Connection, paths: list[str], mode: str) -> Requested:
    """Add a journal row for each online-only file among paths."""
    if mode not in MODES:
        raise ValueError(f"mode must be 1 of {', '.join(MODES)}")
    queued = size = not_online = no_text = 0
    with conn.transaction():
        for fid, path, ext, fsize, state in conn.execute(
            # A zip entry is not a file on the disk, and is never down-loaded.
            "SELECT id, path, ext, size, cloud FROM files WHERE path = ANY(%s) "
            "AND parent_id IS NULL ORDER BY path", (list(paths),)
        ).fetchall():
            if not wants_content(ext):
                no_text += 1
                continue
            if state != "online":
                not_online += 1
                continue
            already = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM fetches WHERE file_id = %s "
                "AND state = ANY(%s))", (fid, list(OPEN_STATES))
            ).fetchone()[0]
            if already:
                continue
            conn.execute(
                "INSERT INTO fetches (file_id, path, size, mode) VALUES (%s, %s, %s, %s)",
                (fid, path, fsize, mode),
            )
            queued += 1
            size += fsize
    return Requested(queued, size, not_online, no_text)


def summary(conn: psycopg.Connection) -> dict:
    """Row counts by state, the size of the queued files, the current limits."""
    counts = {s: 0 for s in OPEN_STATES + ("done", "skipped")}
    for state, n in conn.execute("SELECT state, count(*) FROM fetches GROUP BY state"):
        counts[state] = n
    (queued_bytes,) = conn.execute(
        "SELECT coalesce(sum(size), 0) FROM fetches WHERE state = 'queued'"
    ).fetchone()
    return counts | {"queued_bytes": int(queued_bytes), "limits": limits().__dict__}


def _set(conn, row_id: int, state: str, note: str | None = None, **extra) -> None:
    cols = ", ".join(f"{k} = %s" for k in extra)
    conn.execute(
        f"UPDATE fetches SET state = %s, note = coalesce(%s, note), updated_at = now()"
        f"{', ' + cols if cols else ''} WHERE id = %s",
        (state, note, *extra.values(), row_id),
    )


def _wait_for(ops: Ops, path: str, want: str, wait: float) -> bool:
    """Wait until the file's state is want ('local' or 'online')."""
    waited = 0.0
    step = 0.5
    while True:
        attrs = ops.attributes(path)
        if attrs is not None and cloud.state_from_attributes(attrs) == want:
            return True
        if waited >= wait:
            return False
        ops.sleep(step)
        waited += step


def _store_text(conn, file_id: int | None, path: str, state: str, attrs) -> str | None:
    """Read the file's text into the index. Returns an error, or None."""
    if file_id is None:
        return "the file left the index"
    row = conn.execute("SELECT ext FROM files WHERE id = %s", (file_id,)).fetchone()
    if row is None:
        return "the file left the index"
    try:
        got = extract(Path(path), row[0])
    except (ExtractError, OSError) as exc:
        conn.execute("UPDATE files SET indexed_at = now() WHERE id = %s", (file_id,))
        return f"text not read: {exc}"
    conn.execute(
        "UPDATE files SET content = %s, content_source = %s, truncated = %s, "
        "cloud = %s, attrs = %s, indexed_at = now() WHERE id = %s",
        (got.text, got.source, got.truncated, state, attrs, file_id),
    )
    return None


def _put_back(conn, ops: Ops, row_id: int, file_id, path: str, wait: float) -> bool:
    """Set a file back to online only. True once that is confirmed."""
    try:
        ops.unpin(path)
    except (OSError, subprocess.CalledProcessError) as exc:
        _set(conn, row_id, "restoring", f"could not set online only yet: {exc}")
        return False
    if not _wait_for(ops, path, "online", wait):
        _set(conn, row_id, "restoring", "not confirmed online only yet")
        return False
    attrs = ops.attributes(path)
    if file_id is not None:
        conn.execute("UPDATE files SET cloud = 'online', attrs = %s WHERE id = %s",
                     (attrs, file_id))
    _set(conn, row_id, "done")
    return True


def run(conn: psycopg.Connection, ops: Ops | None = None, limit: int | None = None,
        lim: Limits | None = None,
        progress: Callable[[str], None] | None = None) -> RunResult:
    """Work through the journal: unfinished rows first, then queued ones.

    Only 1 run goes at a time. A 2nd run could otherwise set a file back to
    online only while the 1st is still reading it.
    """
    lim = lim or limits()
    free_floor, max_size, wait = lim.free_floor, lim.max_size, float(lim.wait_seconds)
    ops = ops or default_ops()
    (got_lock,) = conn.execute("SELECT pg_try_advisory_lock(%s)", (LOCK_KEY,)).fetchone()
    if not got_lock:
        return RunResult(0, 0, 0, "another run is in progress")
    try:
        return _run(conn, ops, limit, lim, free_floor, max_size, wait, progress)
    finally:
        conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))


def _run(conn, ops: Ops, limit, lim: Limits, free_floor, max_size, wait,
         progress) -> RunResult:
    done = skipped = handled = 0
    stopped = None
    passed: set[int] = set()  # rows left open in this run, not retried in it
    while limit is None or handled < limit:
        row = _claim_next(conn, passed)
        if row is None:
            break
        row_id, file_id, path, size, mode, state = row
        handled += 1
        if progress:
            progress(f"{state} {mode} {path}")

        if state == "restoring":
            if _put_back(conn, ops, row_id, file_id, path, wait):
                done += 1
            else:
                passed.add(row_id)
            continue

        if state == "queued":
            if size > max_size:
                _set(conn, row_id, "skipped", f"larger than {lim.max_file_mb} MB")
                skipped += 1
                continue
            if ops.free_bytes(path) - size < free_floor:
                stopped = f"free space would fall under {lim.free_floor_mb} MB"
                break
            earlier = ops.attributes(path)
            if earlier is None:
                _set(conn, row_id, "skipped", "the file is gone")
                skipped += 1
                continue
            if cloud.state_from_attributes(earlier) != "online":
                # Already on the disk: read it, and change nothing.
                with conn.transaction():
                    err = _store_text(conn, file_id, path, "local", earlier)
                    _set(conn, row_id, "done", err or "already on this disk",
                         earlier_attrs=earlier)
                done += 1
                continue
            # The earlier state is committed before anything changes it.
            _set(conn, row_id, "downloading", None, earlier_attrs=earlier)

        # state is downloading: pin, wait, read
        try:
            ops.pin(path)
        except (OSError, subprocess.CalledProcessError) as exc:
            if mode == "temporary":
                _set(conn, row_id, "restoring", f"could not down-load: {exc}")
                if _put_back(conn, ops, row_id, file_id, path, wait):
                    done += 1
                else:
                    passed.add(row_id)
            else:
                _set(conn, row_id, "skipped", f"could not down-load: {exc}")
                skipped += 1
            continue
        here = _wait_for(ops, path, "local", wait)
        err = None
        if here:
            attrs = ops.attributes(path)
            err = _store_text(conn, file_id, path, "local", attrs)
        else:
            err = "the down-load did not finish in time"
        if mode == "keep":
            if here:
                _set(conn, row_id, "done", err)
                done += 1
            else:
                _set(conn, row_id, "downloading", err)
                passed.add(row_id)
            continue
        _set(conn, row_id, "restoring", err)
        if _put_back(conn, ops, row_id, file_id, path, wait):
            done += 1
        else:
            passed.add(row_id)

    (waiting,) = conn.execute(
        "SELECT count(*) FROM fetches WHERE state IN ('downloading', 'restoring')"
    ).fetchone()
    return RunResult(done, skipped, waiting, stopped)


def _claim_next(conn, passed: set[int]):
    return conn.execute(
        "SELECT id, file_id, path, size, mode, state FROM fetches "
        "WHERE state = ANY(%s) AND NOT (id = ANY(%s)) ORDER BY "
        # Unfinished rows first: they put files back before new ones come down.
        "CASE state WHEN 'restoring' THEN 0 WHEN 'downloading' THEN 1 ELSE 2 END, id "
        "LIMIT 1", (list(OPEN_STATES), list(passed))
    ).fetchone()
