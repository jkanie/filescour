"""Every search must give exactly what a plain scan of the same files gives.

A seeded random tree is indexed once. Hypothesis then draws a mode, a query
and any mix of filters, and the result is compared with a slow, obvious
Python scan of the folder that applies the same rules.
"""

from __future__ import annotations

import os
import random
import re
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from filescour import db, extract, indexer, jobs
from filescour.search import LINE_CHARS, LINES_PER_HIT, Filters, search

from .conftest import wipe
from .helpers import write

VOCAB = ["alpha", "beta", "gamma", "delta", "invoice", "paid", "error",
         "warning", "total", "2026", "4417", "x9"]
DIRS = ["docs", "docs2", "docs/old", "code", "Photos", "mixed case"]
BASES = ["report", "Notes", "todo", "main", "Budget", "readme", "data",
         "log", "Draft", "photo", "scan", "q3"]
EXTS = ["txt", "md", "py", "csv", "TXT", "bin", "jpg"]
T0 = 1_700_000_000


@pytest.fixture(scope="module")
def tree(tmp_path_factory, test_url):
    rng = random.Random(7)
    root = tmp_path_factory.mktemp("tree")
    for i in range(70):
        ext = rng.choice(EXTS)
        name = f"{rng.choice(BASES)}_{i}.{ext}"
        path = root / rng.choice(DIRS) / name
        if ext in ("bin", "jpg"):
            data = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40))) + b"\x00"
        elif i % 23 == 0:
            data = b"caf\xe9 not utf-8"
        else:
            lines = [" ".join(rng.choice(VOCAB) for _ in range(rng.randrange(1, 6)))
                     for _ in range(rng.randrange(1, 9))]
            data = ("\n".join(lines) + "\n").encode()
        ns = (T0 + rng.randrange(60_000_000)) * 1_000_000_000 + rng.randrange(10**9)
        write(path, data, mtime_ns=ns)
    with db.connect(test_url) as conn:
        wipe(conn)
        rid = indexer.add_root(conn, str(root))
        indexer.scan_root(conn, rid)
        jobs.run(conn)
    return os.path.realpath(root)


def plain_scan(root: str) -> list[dict]:
    out = []
    for path, name, stt in indexer.walk(root):
        ext = indexer.ext_of(name)
        content = None
        if extract.wants_content(ext):
            try:
                content = extract.extract(__import__("pathlib").Path(path), ext).text
            except extract.ExtractError:
                content = None
        out.append({"path": path, "name": name, "ext": ext, "size": stt.st_size,
                    "mtime": indexer.mtime_of(stt), "content": content})
    return out


def expected(root, mode, q, ignore_case, f: Filters):
    flags = re.IGNORECASE if ignore_case else 0
    rows = []
    for r in plain_scan(root):
        if f.folder and not r["path"].startswith(f.folder.rstrip("/") + "/"):
            continue
        if f.exts and r["ext"] not in f.exts:
            continue
        if f.min_size is not None and r["size"] < f.min_size:
            continue
        if f.max_size is not None and r["size"] > f.max_size:
            continue
        if f.modified_after is not None and r["mtime"] < f.modified_after:
            continue
        if f.modified_before is not None and r["mtime"] >= f.modified_before:
            continue
        if f.source == "file" and r["content"] is None:
            continue
        if f.source == "none" and r["content"] is not None:
            continue
        if f.source == "pdf":
            continue
        lines = []
        if mode in ("name", "path"):
            hay = r["name"] if mode == "name" else r["path"]
            if q and not ((q.lower() in hay.lower()) if ignore_case else (q in hay)):
                continue
        elif mode == "regex":
            if r["content"] is None:
                continue
            lines = [(n, t[:LINE_CHARS]) for n, t in
                     enumerate(r["content"].split("\n"), start=1)
                     if re.search(q, t, flags)]
            if not lines:
                continue
            lines = lines[:LINES_PER_HIT]
        else:
            if r["content"] is None:
                continue
            tokens = set(re.findall(r"[a-z0-9]+", r["content"].lower()))
            if not all(w in tokens for w in q.lower().split()):
                continue
        rows.append((r["path"], lines))
    return sorted(rows)


QUERIES = {
    "name": ["", "report", "NOTES", "_1", "a", "q3", "Draft_", "zzz"],
    "path": ["docs", "/docs/", "docs2", "mixed case", "old/", "Photos", "ode/m"],
    "regex": ["invoice", "^alpha", "paid$", "[0-9]{4}", "err(or)?", "a.p",
              "gamma beta", "x9|4417", "^$", "^[a-z]+$"],
    "words": ["invoice", "alpha beta", "total 2026", "warning", "x9", "nothing"],
}
STAMP = st.integers(T0, T0 + 60_000_000).map(
    lambda s: datetime.fromtimestamp(s, tz=timezone.utc))


@st.composite
def cases(draw):
    mode = draw(st.sampled_from(list(QUERIES)))
    q = draw(st.sampled_from(QUERIES[mode]))
    ignore_case = draw(st.booleans())
    def maybe(strategy, empty=None):
        # Each filter is used about 1 time in 4, such that most cases still
        # have hits to compare and every mix of filters still turns up.
        return draw(strategy) if draw(st.integers(0, 3)) == 0 else empty

    filt = dict(
        folder=maybe(st.sampled_from(DIRS + ["docs/"])),
        exts=tuple(maybe(st.lists(st.sampled_from(["txt", "md", "py", "csv", "bin", "jpg"]),
                                  min_size=1, max_size=3, unique=True), [])),
        min_size=maybe(st.integers(0, 60)),
        max_size=maybe(st.integers(0, 120)),
        modified_after=maybe(STAMP),
        modified_before=maybe(STAMP),
        source=maybe(st.sampled_from(["file", "none", "pdf"])),
    )
    return mode, q, ignore_case, filt


@settings(max_examples=300, deadline=None,
          suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(case=cases())
def test_search_matches_a_plain_scan(tree, test_url, case):
    mode, q, ignore_case, filt = case
    if filt["folder"]:
        filt["folder"] = os.path.join(tree, filt["folder"])
    f = Filters(**filt)
    with db.connect(test_url) as conn:
        result = search(conn, q, mode, f, ignore_case, limit=1000)
    want = expected(tree, mode, q, ignore_case, f)
    got = [(h.path, [(ln.line, ln.text) for ln in h.lines] if mode == "regex" else [])
           for h in result.hits]
    assert got == want
    assert result.total == len(want)


def test_folder_filter_does_not_match_a_longer_sibling(tree, test_url):
    with db.connect(test_url) as conn:
        r = search(conn, "", "name", Filters(folder=os.path.join(tree, "docs")))
    assert r.hits
    assert all(h.path.startswith(os.path.join(tree, "docs") + "/") for h in r.hits)
    assert not any("/docs2/" in h.path for h in r.hits)


def test_the_tree_covers_every_kind_of_file(tree):
    kinds = {("content" if r["content"] is not None else "none", r["ext"])
             for r in plain_scan(tree)}
    assert ("none", "txt") in kinds          # a text file that is not UTF-8
    assert ("none", "jpg") in kinds          # a type with no text reader
    assert ("content", "txt") in kinds       # .TXT is read as txt
    assert any(r["name"].endswith(".TXT") for r in plain_scan(tree))


def test_date_and_size_bounds_at_the_exact_value(tree, test_url):
    """modified_after and min/max size include their bound, modified_before does not."""
    rows = plain_scan(tree)
    with db.connect(test_url) as conn:
        for r in rows[:25]:
            def paths(**kw):
                return {h.path for h in search(conn, "", "name", Filters(**kw), limit=1000).hits}
            assert r["path"] in paths(modified_after=r["mtime"])
            assert r["path"] not in paths(modified_before=r["mtime"])
            assert r["path"] in paths(modified_before=r["mtime"] + timedelta(microseconds=1))
            assert r["path"] not in paths(modified_after=r["mtime"] + timedelta(microseconds=1))
            assert r["path"] in paths(min_size=r["size"], max_size=r["size"])
            assert r["path"] not in paths(min_size=r["size"] + 1)
