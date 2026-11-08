import json

from filescour import export, indexer, jobs
from filescour.search import Filters, search

from .helpers import write

NS = 1_717_171_717_123_456_000  # 2024-05-31T16:08:37.123456Z


def build(conn, tmp_path):
    write(tmp_path / "a" / "ledger.csv", "date,total\n2026-01-02,4417\n", mtime_ns=NS)
    write(tmp_path / "a" / "photo.jpg", b"\xff\xd8\x00", mtime_ns=NS)
    write(tmp_path / "b" / "notes.txt", 'He said "4417, paid"\n', mtime_ns=NS)
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)
    jobs.run(conn)
    return str(tmp_path)


def test_csv_export(conn, tmp_path):
    root = build(conn, tmp_path)
    result = search(conn, "4417", "regex", Filters(exts=("csv", "txt")))
    head = ('# filescour search: {"filters": {"exts": ["csv", "txt"]}, '
            '"ignore_case": true, "mode": "regex", "query": "4417", '
            '"returned": 2, "total": 2}\n')
    assert export.to_csv(result) == (
        head
        + "path,name,ext,size,modified,source,cloud,line,text\n"
        + f"{root}/a/ledger.csv,ledger.csv,csv,27,2024-05-31T16:08:37.123456+00:00,file,local,2,\"2026-01-02,4417\"\n"
        + f"{root}/b/notes.txt,notes.txt,txt,21,2024-05-31T16:08:37.123456+00:00,file,local,1,\"He said \"\"4417, paid\"\"\"\n"
    )


def test_csv_export_of_a_name_search_has_1_row_per_file(conn, tmp_path):
    root = build(conn, tmp_path)
    result = search(conn, "photo", "name")
    lines = export.to_csv(result).splitlines()
    assert lines[2:] == [f"{root}/a/photo.jpg,photo.jpg,jpg,3,2024-05-31T16:08:37.123456+00:00,,local,,"]


def test_json_export(conn, tmp_path):
    root = build(conn, tmp_path)
    result = search(conn, "paid", "words", Filters(folder=f"{root}/b"))
    doc = json.loads(export.to_json(result))
    assert doc == {
        "search": {"filters": {"folder": f"{root}/b"}, "ignore_case": True,
                   "mode": "words", "query": "paid", "returned": 1, "total": 1},
        "hits": [{"path": f"{root}/b/notes.txt", "name": "notes.txt", "ext": "txt",
                  "size": 21, "modified": "2024-05-31T16:08:37.123456+00:00",
                  "source": "file", "cloud": "local", "truncated": False,
                  "lines": [{"line": None, "text": 'He said "4417, paid"'}]}],
    }


def test_json_export_is_stable(conn, tmp_path):
    build(conn, tmp_path)
    result = search(conn, "", "name")
    assert export.to_json(result) == export.to_json(search(conn, "", "name"))
    assert export.to_json(result).endswith("}\n")
