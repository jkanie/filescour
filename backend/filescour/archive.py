"""List the entries of a zip file as rows of their own, with their text.

Each entry gets a row whose path is the zip's path, then "!/", then the
entry's name inside the zip, such as /home/me/backup.zip!/notes/may.txt.
Its parent_id points at the zip's row. A scan deletes the entries of a changed
zip, and deleting a zip's row deletes its entries. An entry's text is read the
same way as a file's (see extract.py), and its modified time is the date
stored in the zip, or the zip's own modified time when that date is not valid.

A worker expands a zip only after the same online-only check as any other
file (see jobs.py), since opening the zip would make a cloud service
down-load it. The guards against hostile zips:

- More than MAX_ENTRIES entries: the zip is not expanded.
- Text-type entries that add up to more than MAX_TOTAL_BYTES uncompressed:
  the zip is not expanded. An entry is read up to 1 byte past its declared
  size at most, and an entry that holds more than it declares gets no text.
- Encrypted entries are listed, but their data is never read.
- A name that could point outside the zip (an absolute path, a drive letter,
  a "." or ".." part, an empty part, a backslash) gets no row.
- A zip inside a zip is listed as an entry, but it is not expanded.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

import psycopg

from .extract import ExtractError, Extracted, extract_bytes, wants_content
from .indexer import ext_of

SEPARATOR = "!/"
MAX_ENTRIES = 10_000
MB = 1024 * 1024
MAX_TOTAL_BYTES = 256 * MB
ENCRYPTED_FLAG = 0x1  # bit 0 of the general purpose flags in a zip header

_DRIVE = re.compile(r"^[A-Za-z]:")


class ArchiveError(Exception):
    """The zip was not expanded: it is unreadable, or it breaks a limit."""


@dataclass(frozen=True)
class Entry:
    name: str  # the path inside the zip, with "/" between folders
    size: int
    mtime: datetime
    text: Extracted | None


@dataclass(frozen=True)
class Expanded:
    entries: int  # rows written
    rejected: int  # names that could point outside the zip
    encrypted: int  # listed, never read


def open_zip(path: str) -> zipfile.ZipFile:
    return zipfile.ZipFile(path)


def safe_name(name: str) -> bool:
    """Whether a name stays inside the zip once joined to its path."""
    if not name or name.startswith("/") or "\\" in name or _DRIVE.match(name):
        return False
    return all(part not in ("", ".", "..") for part in name.split("/"))


def _hidden(name: str) -> bool:
    # The scan skips names that start with a dot, and so does this.
    return any(part.startswith(".") for part in name.split("/"))


def _mtime(info: zipfile.ZipInfo, fallback: datetime) -> datetime:
    # A zip stores a local date and time with no time zone. It is kept as UTC.
    try:
        return datetime(*info.date_time, tzinfo=timezone.utc)
    except ValueError:
        return fallback


def _encrypted(info: zipfile.ZipInfo) -> bool:
    return bool(info.flag_bits & ENCRYPTED_FLAG)


def plan(zf: zipfile.ZipFile) -> tuple[list[zipfile.ZipInfo], int, int]:
    """The entries to list, with the counts of rejected and encrypted ones.

    Raises ArchiveError when the zip breaks a limit, before any entry is read.
    """
    infos = zf.infolist()
    if len(infos) > MAX_ENTRIES:
        raise ArchiveError(f"more than {MAX_ENTRIES} entries")
    keep, rejected, encrypted, to_read = [], 0, 0, 0
    for info in infos:
        if info.is_dir():
            continue
        if not safe_name(info.filename):
            rejected += 1
            continue
        if _hidden(info.filename):
            continue
        keep.append(info)
        if _encrypted(info):
            encrypted += 1
        elif wants_content(ext_of(info.filename)):
            to_read += info.file_size
    if to_read > MAX_TOTAL_BYTES:
        raise ArchiveError(
            f"text-type entries hold more than {MAX_TOTAL_BYTES // MB} MB uncompressed"
        )
    return keep, rejected, encrypted


def _text(zf: zipfile.ZipFile, info: zipfile.ZipInfo) -> Extracted | None:
    ext = ext_of(info.filename)
    if _encrypted(info) or not wants_content(ext):
        return None
    try:
        with zf.open(info) as fh:
            data = fh.read(info.file_size + 1)
    except Exception:  # zipfile, zlib, bz2, lzma raise many types for broken data
        return None
    if len(data) > info.file_size:
        return None
    try:
        return extract_bytes(data, ext)
    except ExtractError:
        return None


def read(zf: zipfile.ZipFile, infos: list[zipfile.ZipInfo],
         fallback: datetime) -> Iterator[Entry]:
    """Each planned entry with its text, 1 at a time."""
    for info in infos:
        yield Entry(info.filename, info.file_size, _mtime(info, fallback),
                    _text(zf, info))


def entry_path(zip_path: str, name: str) -> str:
    return zip_path + SEPARATOR + name


def expand(conn: psycopg.Connection, file_id: int, path: str, mtime: datetime,
           state: str) -> Expanded:
    """Replace the entry rows of the zip at path. Run it inside a transaction.

    Raises ArchiveError when the zip cannot be read or breaks a limit. The
    caller's transaction then rolls back every row written so far.
    """
    try:
        with open_zip(path) as zf:
            infos, rejected, encrypted = plan(zf)
            conn.execute("DELETE FROM files WHERE parent_id = %s", (file_id,))
            written = 0
            for e in read(zf, infos, mtime):
                name = e.name.rsplit("/", 1)[-1]
                t = e.text
                # A 2nd entry with the same name, or a real file at the same
                # path, keeps the row that is already there.
                cur = conn.execute(
                    "INSERT INTO files (root_id, parent_id, path, name, ext, size, mtime, "
                    "content, content_source, truncated, cloud, indexed_at) "
                    "SELECT root_id, id, %s, %s, %s, %s, %s, %s, %s, %s, %s, now() "
                    "FROM files WHERE id = %s ON CONFLICT (path) DO NOTHING",
                    (entry_path(path, e.name), name, ext_of(name), e.size, e.mtime,
                     t.text if t else None, t.source if t else None,
                     t.truncated if t else False, state, file_id),
                )
                written += cur.rowcount
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"not a readable zip: {exc}") from exc
    return Expanded(written, rejected, encrypted)
