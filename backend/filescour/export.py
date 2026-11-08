"""Write search results to CSV or JSON.

Both formats record the query and filters that produced the results, so an
exported file says on its own what it is a list of.
"""

from __future__ import annotations

import csv
import io
import json

from .search import Result

CSV_COLUMNS = ["path", "name", "ext", "size", "modified", "source", "cloud", "line", "text"]


def query_header(result: Result) -> dict:
    return {
        "mode": result.mode,
        "query": result.query,
        "ignore_case": result.ignore_case,
        "filters": result.filters.as_dict(),
        "total": result.total,
        "returned": len(result.hits),
    }


def to_json(result: Result) -> str:
    doc = {
        "search": query_header(result),
        "hits": [
            {
                "path": h.path, "name": h.name, "ext": h.ext, "size": h.size,
                "modified": h.modified, "source": h.source, "cloud": h.cloud,
                "truncated": h.truncated,
                "lines": [{"line": ln.line, "text": ln.text} for ln in h.lines],
            }
            for h in result.hits
        ],
    }
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def to_csv(result: Result) -> str:
    """1 row per matching line, or 1 row per file when there is no line.

    The search itself goes on comment lines that start with '#', ahead of the
    header row.
    """
    out = io.StringIO()
    header = query_header(result)
    out.write(f"# filescour search: {json.dumps(header, sort_keys=True, ensure_ascii=False)}\n")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for h in result.hits:
        base = [h.path, h.name, h.ext, h.size, h.modified, h.source or "", h.cloud]
        if h.lines:
            for ln in h.lines:
                writer.writerow(base + ["" if ln.line is None else ln.line, ln.text])
        else:
            writer.writerow(base + ["", ""])
    return out.getvalue()
