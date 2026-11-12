"""Search the index by name, path, regular expression or words.

Every mode takes the same filters, and every filter is 1 condition in the
query, so filters combine freely. Regular expressions use PostgreSQL's own
engine, both to pick files and to pick matching lines, so the 2 always agree.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime

import psycopg
from psycopg import errors

MODES = ("name", "path", "regex", "words")
SOURCES = ("file", "pdf", "doc", "none")
CLOUDS = ("local", "online", "unknown")
MAX_LIMIT = 1000
# Sort keys and their columns. A leading "-" sorts high to low. Ties fall
# back to the path, A to Z, so every order is fixed.
SORT_COLUMNS = {
    "path": 'f.path COLLATE "C"',
    "name": 'f.name COLLATE "C"',
    "size": "f.size",
    "modified": "f.mtime",
}
SORTS = tuple(k for key in SORT_COLUMNS for k in (key, "-" + key))
LINES_PER_HIT = 5
LINE_CHARS = 300


@dataclass(frozen=True)
class Filters:
    folder: str | None = None
    exts: tuple[str, ...] = ()
    min_size: int | None = None
    max_size: int | None = None
    modified_after: datetime | None = None
    modified_before: datetime | None = None
    source: str | None = None
    cloud: str | None = None

    def as_dict(self) -> dict:
        out = {}
        for key, value in asdict(self).items():
            if value in (None, (), []):
                continue
            if isinstance(value, datetime):
                value = value.isoformat()
            if isinstance(value, tuple):
                value = list(value)
            out[key] = value
        return out


@dataclass(frozen=True)
class Line:
    line: int | None
    text: str


@dataclass(frozen=True)
class Hit:
    path: str
    name: str
    ext: str
    size: int
    modified: str
    source: str | None
    cloud: str
    truncated: bool
    archive: str | None  # the path of the zip that holds this entry, or None
    lines: list[Line] = field(default_factory=list)


@dataclass(frozen=True)
class Result:
    mode: str
    query: str
    ignore_case: bool
    filters: Filters
    total: int
    hits: list[Hit]
    sort: str = "path"


class BadQuery(ValueError):
    """The query or a filter cannot be run as given."""


def _line(x: dict) -> Line:
    if x["line"] is None:  # a words-mode snippet: fold it onto 1 line
        return Line(None, " ".join(x["text"].split()))
    return Line(x["line"], x["text"].rstrip("\r"))


def _like_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def norm_exts(values) -> tuple[str, ...]:
    out = []
    for value in values:
        for part in str(value).split(","):
            part = part.strip().lstrip(".").lower()
            if part and part not in out:
                out.append(part)
    return tuple(out)


def _filter_sql(f: Filters) -> tuple[list[str], list]:
    where, args = [], []
    if f.folder:
        folder = f.folder.rstrip("/\\") + os.sep
        where.append("f.path LIKE %s")
        args.append(_like_escape(folder) + "%")
    if f.exts:
        where.append("f.ext = ANY(%s)")
        args.append(list(f.exts))
    if f.min_size is not None:
        where.append("f.size >= %s")
        args.append(f.min_size)
    if f.max_size is not None:
        where.append("f.size <= %s")
        args.append(f.max_size)
    if f.modified_after is not None:
        where.append("f.mtime >= %s")
        args.append(f.modified_after)
    if f.modified_before is not None:
        where.append("f.mtime < %s")
        args.append(f.modified_before)
    if f.source is not None:
        if f.source not in SOURCES:
            raise BadQuery(f"source must be 1 of {', '.join(SOURCES)}")
        if f.source == "none":
            where.append("f.content_source IS NULL")
        else:
            where.append("f.content_source = %s")
            args.append(f.source)
    if f.cloud is not None:
        if f.cloud not in CLOUDS:
            raise BadQuery(f"cloud must be 1 of {', '.join(CLOUDS)}")
        where.append("f.cloud = %s")
        args.append(f.cloud)
    return where, args


def search(conn: psycopg.Connection, q: str, mode: str = "name",
           filters: Filters = Filters(), ignore_case: bool = True,
           limit: int = 200, sort: str = "path") -> Result:
    if mode not in MODES:
        raise BadQuery(f"mode must be 1 of {', '.join(MODES)}")
    if sort not in SORTS:
        raise BadQuery(f"sort must be 1 of {', '.join(SORTS)}")
    order = SORT_COLUMNS[sort.lstrip("-")] + (" DESC" if sort.startswith("-") else "")
    if sort.lstrip("-") != "path":
        order += ', f.path COLLATE "C"'
    if not 1 <= limit <= MAX_LIMIT:
        raise BadQuery(f"limit must be from 1 to {MAX_LIMIT}")
    where, args = _filter_sql(filters)
    lines_sql, lines_args = "NULL::json", []

    if mode in ("name", "path"):
        if q:
            col = "f.name" if mode == "name" else "f.path"
            where.append(f"{col} {'ILIKE' if ignore_case else 'LIKE'} %s")
            args.append("%" + _like_escape(q) + "%")
    elif mode == "regex":
        if not q:
            raise BadQuery("a regular expression is needed for regex mode")
        op = "~*" if ignore_case else "~"
        # The whole-text test lets the trigram index narrow the files first.
        # It runs newline-sensitive ("(?n)"), so ^ and $ match at every line
        # and it keeps every file that the line test below can keep.
        # A pattern that sets its own options skips this narrowing step, and
        # so does 1 with \A or \Z, which match only at the very start and end
        # of the whole text even in newline-sensitive mode.
        if not q.startswith(("(?", "***")) and "\\A" not in q and "\\Z" not in q:
            where.append(f"f.content {op} %s")
            args.append("(?n)" + q)
        where.append(
            "EXISTS (SELECT 1 FROM regexp_split_to_table(f.content, E'\\n') AS l "
            f"WHERE l {op} %s)"
        )
        args.append(q)
        lines_sql = (
            "(SELECT json_agg(json_build_object('line', n, 'text', left(t, %s)) ORDER BY n) "
            "FROM (SELECT n, t FROM regexp_split_to_table(f.content, E'\\n') "
            f"WITH ORDINALITY AS s(t, n) WHERE t {op} %s ORDER BY n LIMIT %s) m)"
        )
        lines_args = [LINE_CHARS, q, LINES_PER_HIT]
    else:  # words
        if not q.strip():
            raise BadQuery("at least 1 word is needed for words mode")
        where.append("f.tsv @@ websearch_to_tsquery('simple', %s)")
        args.append(q)
        lines_sql = (
            "json_build_array(json_build_object('line', NULL, 'text', "
            "ts_headline('simple', left(f.content, 500000), "
            "websearch_to_tsquery('simple', %s), "
            "'StartSel=\"\", StopSel=\"\", MaxWords=25, MinWords=8')))"
        )
        lines_args = [q]

    where_sql = " AND ".join(where) if where else "TRUE"
    try:
        with conn.transaction():
            total = conn.execute(
                f"SELECT count(*) FROM files f WHERE {where_sql}", args
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT f.path, f.name, f.ext, f.size, f.mtime, f.content_source, f.cloud, "
                f"f.truncated, z.path, {lines_sql} FROM files f "
                f"LEFT JOIN files z ON z.id = f.parent_id WHERE {where_sql} "
                f"ORDER BY {order} LIMIT %s",
                lines_args + args + [limit],
            ).fetchall()
    except errors.InvalidRegularExpression as exc:
        raise BadQuery(f"invalid regular expression: {exc.diag.message_primary}") from exc

    hits = []
    for path, name, ext, size, mtime, source, state, truncated, archive, lines in rows:
        if isinstance(lines, str):
            lines = json.loads(lines)
        hits.append(Hit(
            path=path, name=name, ext=ext, size=size,
            modified=mtime.isoformat(), source=source, cloud=state,
            truncated=truncated, archive=archive,
            lines=[_line(x) for x in (lines or [])],
        ))
    return Result(mode, q, ignore_case, filters, total, hits, sort)
