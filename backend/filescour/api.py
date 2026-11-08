"""HTTP API for the search screen."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from . import __version__, db, export, fetch, indexer, jobs
from .search import MAX_LIMIT, BadQuery, Filters, norm_exts, search

ORIGINS_ENV = "FILESCOUR_CORS_ORIGINS"


def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with db.connect() as conn:
        db.init_schema(conn)
    yield


app = FastAPI(title="filescour", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get(ORIGINS_ENV, "http://localhost:3000").split(","),
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["*"],
)


class RootIn(BaseModel):
    path: str
    extract_unknown: bool = False


class FetchIn(BaseModel):
    paths: list[str] = Field(min_length=1, max_length=MAX_LIMIT)
    mode: Literal["temporary", "keep"] = "temporary"


def _filters(
    folder: str | None = None,
    ext: list[str] = Query(default=[]),
    min_size: int | None = Query(default=None, ge=0),
    max_size: int | None = Query(default=None, ge=0),
    modified_after: datetime | None = None,
    modified_before: datetime | None = None,
    source: Literal["file", "pdf", "none"] | None = None,
    cloud: Literal["local", "online", "unknown"] | None = None,
) -> Filters:
    return Filters(folder=folder or None, exts=norm_exts(ext),
                   min_size=min_size, max_size=max_size,
                   modified_after=modified_after,
                   modified_before=modified_before, source=source, cloud=cloud)


def _run(conn, q, mode, filters, ignore_case, limit):
    try:
        return search(conn, q, mode, filters, ignore_case, limit)
    except BadQuery as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/health")
def health():
    return {"status": "ok", "version": __version__}


@app.get("/roots")
def list_roots(conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT r.id, r.path, r.extract_unknown, count(f.id), "
        "count(f.id) FILTER (WHERE f.cloud = 'online'), "
        "count(f.id) FILTER (WHERE f.cloud = 'unknown') FROM roots r "
        "LEFT JOIN files f ON f.root_id = r.id GROUP BY r.id ORDER BY r.id"
    ).fetchall()
    return [{"id": i, "path": p, "extract_unknown": x, "files": n,
             "online": on, "unknown": un} for i, p, x, n, on, un in rows]


@app.post("/roots", status_code=201)
def create_root(body: RootIn, conn=Depends(get_conn)):
    try:
        root_id = indexer.add_root(conn, body.path, body.extract_unknown)
    except NotADirectoryError as exc:
        raise HTTPException(status_code=422, detail=f"not a folder: {exc}") from exc
    return {"id": root_id}


@app.delete("/roots/{root_id}", status_code=204)
def delete_root(root_id: int, conn=Depends(get_conn)):
    conn.execute("DELETE FROM roots WHERE id = %s", (root_id,))


@app.post("/roots/{root_id}/scan")
def scan(root_id: int, conn=Depends(get_conn)):
    try:
        result = indexer.scan_root(conn, root_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return result.__dict__


@app.get("/jobs")
def job_summary(conn=Depends(get_conn)):
    return jobs.summary(conn)


@app.post("/jobs/run")
def run_jobs(limit: int = Query(default=100, ge=1, le=10_000), conn=Depends(get_conn)):
    return {"processed": jobs.run(conn, limit), **jobs.summary(conn)}


@app.get("/fetch")
def fetch_summary(conn=Depends(get_conn)):
    return fetch.summary(conn)


@app.post("/fetch")
def fetch_request(body: FetchIn, conn=Depends(get_conn)):
    return fetch.request(conn, body.paths, body.mode).__dict__


@app.post("/fetch/run")
def fetch_run(
    limit: int = Query(default=100, ge=1, le=MAX_LIMIT),
    free_floor_mb: int | None = Query(default=None, ge=0),
    max_file_mb: int | None = Query(default=None, ge=0),
    wait_seconds: int | None = Query(default=None, ge=0),
    conn=Depends(get_conn),
):
    try:
        lim = fetch.limits(free_floor_mb=free_floor_mb, max_file_mb=max_file_mb,
                           wait_seconds=wait_seconds)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        result = fetch.run(conn, limit=limit, lim=lim)
    except fetch.FetchUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result.__dict__ | {"totals": fetch.summary(conn)}


@app.get("/search")
def do_search(
    q: str = "",
    mode: Literal["name", "path", "regex", "words"] = "name",
    ignore_case: bool = True,
    limit: int = Query(default=200, ge=1, le=MAX_LIMIT),
    filters: Filters = Depends(_filters),
    conn=Depends(get_conn),
):
    result = _run(conn, q, mode, filters, ignore_case, limit)
    return {
        "search": export.query_header(result),
        "hits": [h.__dict__ | {"lines": [ln.__dict__ for ln in h.lines]}
                 for h in result.hits],
    }


@app.get("/export")
def do_export(
    format: Literal["csv", "json"] = "csv",
    q: str = "",
    mode: Literal["name", "path", "regex", "words"] = "name",
    ignore_case: bool = True,
    limit: int = Query(default=MAX_LIMIT, ge=1, le=MAX_LIMIT),
    filters: Filters = Depends(_filters),
    conn=Depends(get_conn),
):
    result = _run(conn, q, mode, filters, ignore_case, limit)
    if format == "json":
        body, media = export.to_json(result), "application/json"
    else:
        body, media = export.to_csv(result), "text/csv"
    return PlainTextResponse(
        body, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="filescour-results.{format}"'},
    )
