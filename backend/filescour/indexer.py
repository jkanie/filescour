"""Walk a folder and bring its rows in the index up to date.

A scan records each file's name, path, size, modified time and cloud state at
once. Text extraction is slower, so it goes onto the job queue (see jobs.py)
and runs in a worker. A file whose size and modified time have not changed
since the last scan keeps its row and its text, and no job is queued for it.

The scan never opens a file. It lists each folder with os.scandir and takes
every file's details from that listing. Text extraction is queued only for a
file whose data is on this disk (see cloud.py).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from . import cloud
from .extract import wants_content

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ScanResult:
    added: int
    changed: int
    unchanged: int
    removed: int
    queued: int
    held: int  # text-type files left unread because their data may be online


def mtime_of(st: os.stat_result) -> datetime:
    """Modified time at microsecond precision, the precision PostgreSQL keeps."""
    return EPOCH + timedelta(microseconds=st.st_mtime_ns // 1000)


def ext_of(name: str) -> str:
    suffix = Path(name).suffix
    return suffix[1:].lower() if suffix else ""


def walk(root: str):
    """Yield (path, name, stat) for every regular file under root.

    Names starting with a dot are skipped, folders included, and symbolic
    links are never followed, so a link loop cannot trap the scan. Each stat
    comes from the folder listing (os.DirEntry.stat), which on Windows needs
    no system call and never opens the file.
    """
    stack = [root]
    while stack:
        folder = stack.pop()
        try:
            with os.scandir(folder) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError:
            continue
        subfolders = []
        for entry in entries:
            if entry.name.startswith("."):
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    subfolders.append(entry.path)
                    continue
                st = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                yield entry.path, entry.name, st
        stack.extend(reversed(subfolders))


def add_root(conn: psycopg.Connection, path: str, extract_unknown: bool = False) -> int:
    real = os.path.realpath(path)
    if not os.path.isdir(real):
        raise NotADirectoryError(real)
    row = conn.execute(
        "INSERT INTO roots (path, extract_unknown) VALUES (%s, %s) "
        "ON CONFLICT (path) DO UPDATE SET extract_unknown = EXCLUDED.extract_unknown "
        "RETURNING id",
        (real, extract_unknown),
    ).fetchone()
    return row[0]


def _queue(conn: psycopg.Connection, file_id: int) -> None:
    conn.execute(
        "DELETE FROM jobs WHERE file_id = %s AND state = 'queued'", (file_id,)
    )
    conn.execute(
        "INSERT INTO jobs (kind, file_id) VALUES ('extract', %s)", (file_id,)
    )


def _has_open_job(conn: psycopg.Connection, file_id: int) -> bool:
    return conn.execute(
        "SELECT EXISTS (SELECT 1 FROM jobs WHERE file_id = %s "
        "AND state IN ('queued', 'running'))", (file_id,)
    ).fetchone()[0]


def scan_root(conn: psycopg.Connection, root_id: int) -> ScanResult:
    row = conn.execute(
        "SELECT path, extract_unknown FROM roots WHERE id = %s", (root_id,)
    ).fetchone()
    if row is None:
        raise KeyError(f"no root with id {root_id}")
    root, extract_unknown = row
    drives = cloud.windows_drives()

    added = changed = unchanged = removed = queued = held = 0
    with conn.transaction():
        known = {
            path: (fid, size, mtime, state, attrs, indexed_at)
            for fid, path, size, mtime, state, attrs, indexed_at in conn.execute(
                "SELECT id, path, size, mtime, cloud, attrs, indexed_at "
                "FROM files WHERE root_id = %s",
                (root_id,),
            )
        }
        seen: set[str] = set()
        for path, name, st in walk(root):
            seen.add(path)
            ext = ext_of(name)
            mtime = mtime_of(st)
            content = wants_content(ext)
            state, attrs = cloud.state_of(st, path, drives)
            readable = content and cloud.may_read(state, extract_unknown)
            old = known.get(path)
            if old is not None and old[1] == st.st_size and old[2] == mtime:
                unchanged += 1
                fid = old[0]
                if (old[3], old[4]) != (state, attrs):
                    conn.execute(
                        "UPDATE files SET cloud = %s, attrs = %s WHERE id = %s",
                        (state, attrs, fid),
                    )
                if content and old[5] is None:  # its text was never read
                    if readable:
                        if not _has_open_job(conn, fid):
                            _queue(conn, fid)
                            queued += 1
                    else:
                        held += 1
                continue
            if old is None:
                fid = conn.execute(
                    "INSERT INTO files (root_id, path, name, ext, size, mtime, cloud, attrs, "
                    "indexed_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, "
                    "CASE WHEN %s THEN NULL ELSE now() END) "
                    "RETURNING id",
                    (root_id, path, name, ext, st.st_size, mtime, state, attrs, content),
                ).fetchone()[0]
                added += 1
            else:
                fid = old[0]
                conn.execute(
                    "UPDATE files SET size = %s, mtime = %s, cloud = %s, attrs = %s, "
                    "content = NULL, content_source = NULL, truncated = false, "
                    "indexed_at = CASE WHEN %s THEN NULL ELSE now() END "
                    "WHERE id = %s",
                    (st.st_size, mtime, state, attrs, content, fid),
                )
                conn.execute(
                    "DELETE FROM jobs WHERE file_id = %s AND state = 'queued'", (fid,)
                )
                changed += 1
            if readable:
                _queue(conn, fid)
                queued += 1
            elif content:
                held += 1
        gone = [fid for path, (fid, *_rest) in known.items() if path not in seen]
        if gone:
            conn.execute("DELETE FROM files WHERE id = ANY(%s)", (gone,))
            removed = len(gone)
    return ScanResult(added, changed, unchanged, removed, queued, held)
