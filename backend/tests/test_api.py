import zipfile

from fastapi.testclient import TestClient

from filescour.api import app

from .helpers import write


def client():
    return TestClient(app)


def test_end_to_end(conn, tmp_path):
    write(tmp_path / "docs" / "plan.md", "budget for 2026\nship in May\n")
    write(tmp_path / "docs" / "scan.jpg", b"\xff\xd8\x00")
    with client() as c:
        assert c.get("/health").json()["status"] == "ok"
        root = c.post("/roots", json={"path": str(tmp_path)})
        assert root.status_code == 201
        assert root.json()["created"] is True
        rid = root.json()["id"]
        # The same folder again (here with a trailing slash) keeps its id: 200, not created.
        again = c.post("/roots", json={"path": str(tmp_path) + "/"})
        assert (again.status_code, again.json()) == (200, {"id": rid, "created": False})
        assert c.post(f"/roots/{rid}/scan").json()["queued"] == 1
        ran = c.post("/jobs/run").json()
        assert (ran["processed"], ran["requeued"], ran["gave_up"], ran["done"]) == (1, 0, 0, 1)
        assert c.get("/roots").json() == [{"id": rid, "path": str(tmp_path),
                                           "extract_unknown": False, "files": 2,
                                           "online": 0, "unknown": 0,
                                           "zip_entries": 0}]
        assert c.get(f"/roots/{rid}").json() == c.get("/roots").json()[0]

        body = c.get("/search", params={"q": "^ship", "mode": "regex"}).json()
        assert body["search"]["total"] == 1
        assert body["hits"][0]["lines"] == [{"line": 2, "text": "ship in May"}]

        body = c.get("/search", params={"q": "", "ext": ["jpg", ".MD"]}).json()
        assert [h["name"] for h in body["hits"]] == ["plan.md", "scan.jpg"]

        body = c.get("/search", params={"q": "", "sort": "-name"}).json()
        assert [h["name"] for h in body["hits"]] == ["scan.jpg", "plan.md"]
        assert body["search"]["sort"] == "-name"
        exp = c.get("/export", params={"format": "json", "sort": "-name"}).json()
        assert [h["name"] for h in exp["hits"]] == ["scan.jpg", "plan.md"]
        assert c.get("/search", params={"sort": "mtime"}).status_code == 422

        page = c.get("/search", params={"q": "", "limit": 1, "offset": 1}).json()
        assert [h["name"] for h in page["hits"]] == ["scan.jpg"]
        assert (page["search"]["offset"], page["search"]["total"]) == (1, 2)
        exp = c.get("/export", params={"format": "json", "limit": 1, "offset": 1}).json()
        assert [h["name"] for h in exp["hits"]] == ["scan.jpg"]
        assert "offset" not in c.get("/search", params={"q": ""}).json()["search"]
        assert c.get("/search", params={"offset": -1}).status_code == 422

        exp = c.get("/export", params={"format": "csv", "q": "budget", "mode": "words"})
        assert exp.status_code == 200
        assert exp.headers["content-type"].startswith("text/csv")
        assert 'filename="filescour-results.csv"' in exp.headers["content-disposition"]
        assert exp.text.splitlines()[1] == "path,name,ext,size,modified,source,cloud,line,text"

        assert c.delete(f"/roots/{rid}").status_code == 204
        assert c.delete(f"/roots/{rid}").status_code == 404
        assert c.get("/search").json()["hits"] == []


def test_bad_input_is_refused(conn, tmp_path):
    with client() as c:
        r = c.get("/search", params={"q": "(", "mode": "regex"})
        assert r.status_code == 422
        assert "invalid regular expression" in r.json()["detail"]
        assert c.get("/search", params={"mode": "regex"}).status_code == 422
        assert c.get("/search", params={"mode": "sql"}).status_code == 422
        assert c.get("/search", params={"min_size": -1}).status_code == 422
        assert c.post("/roots", json={"path": str(tmp_path / "missing")}).status_code == 422
        assert c.post("/roots/999/scan").status_code == 404
        assert c.delete("/roots/999").json() == {"detail": "no folder with id 999"}
        assert c.get("/roots/999").status_code == 404
        assert c.get("/roots/999").json() == {"detail": "no folder with id 999"}


def test_jobs_count_per_folder(conn, tmp_path):
    write(tmp_path / "a" / "1.txt", "one")
    write(tmp_path / "a" / "2.txt", "two")
    write(tmp_path / "b" / "3.txt", "three")
    with client() as c:
        a = c.post("/roots", json={"path": str(tmp_path / "a")}).json()["id"]
        b = c.post("/roots", json={"path": str(tmp_path / "b")}).json()["id"]
        assert c.post(f"/roots/{a}/scan").json()["queued"] == 2
        assert c.post(f"/roots/{b}/scan").json()["queued"] == 1
        assert c.post("/jobs/run", params={"limit": 1}).json()["processed"] == 1
        everything = c.get("/jobs").json()
        per = [c.get("/jobs", params={"root": r}).json() for r in (a, b)]
        assert everything["queued"] + everything["done"] == 3
        assert sum(p["queued"] for p in per) == everything["queued"]
        assert sum(p["done"] for p in per) == everything["done"]
        assert [p["queued"] + p["done"] for p in per] == [2, 1]
        assert per[0].keys() == everything.keys()
        missing = c.get("/jobs", params={"root": max(a, b) + 1})
        assert missing.status_code == 404
        assert missing.json()["detail"] == f"no folder with id {max(a, b) + 1}"
        assert c.get("/jobs", params={"root": "x"}).status_code == 422


def test_failed_jobs_name_the_file_and_the_reason(conn, tmp_path):
    write(tmp_path / "good.txt", "fine")
    (tmp_path / "bad.txt").write_bytes(b"caf\xe9")
    (tmp_path / "worse.txt").write_bytes(b"\x80\x81")
    with client() as c:
        root = c.post("/roots", json={"path": str(tmp_path)}).json()["id"]
        assert c.post(f"/roots/{root}/scan").json()["queued"] == 3
        assert c.get("/jobs/failed").json() == []
        assert c.post("/jobs/run").json()["processed"] == 3
        failed = c.get("/jobs/failed").json()
        assert sorted(f["path"] for f in failed) == [str(tmp_path / "bad.txt"), str(tmp_path / "worse.txt")]
        assert {f["error"] for f in failed} == {"not UTF-8 text"}
        assert all(f["attempts"] == 1 for f in failed)
        # The most recent 1st.
        assert failed[0]["updated_at"] >= failed[1]["updated_at"]
        assert c.get("/jobs").json()["failed"] == 2
        assert len(c.get("/jobs/failed", params={"limit": 1}).json()) == 1
        assert c.get("/jobs/failed", params={"limit": 0}).status_code == 422
        assert c.get("/jobs/failed", params={"limit": 1001}).status_code == 422


def test_extensions_of_1_folder(conn, tmp_path):
    write(tmp_path / "a.txt", "12345")
    write(tmp_path / "docs" / "B.TXT", "123")
    write(tmp_path / "docs" / "plan.md", "1")
    write(tmp_path / "Makefile", "all:\n")
    zf = tmp_path / "old.zip"
    with zipfile.ZipFile(zf, "w") as z:
        z.writestr("notes/may.md", "12")
        z.writestr("readme.txt", "1234567")
    other = tmp_path.parent / (tmp_path.name + "-other")
    write(other / "c.txt", "x")
    with client() as c:
        rid = c.post("/roots", json={"path": str(tmp_path)}).json()["id"]
        oid = c.post("/roots", json={"path": str(other)}).json()["id"]
        assert c.get(f"/roots/{rid}/extensions").json() == []
        c.post(f"/roots/{rid}/scan")
        c.post(f"/roots/{oid}/scan")
        # The jobs read the zip's entries into the index.
        c.post("/jobs/run")
        # The zip's 2 entries count, and B.TXT counts as txt. The other folder's file does not.
        assert c.get(f"/roots/{rid}/extensions").json() == [
            {"ext": "txt", "files": 3, "bytes": 15},
            {"ext": "md", "files": 2, "bytes": 3},
            {"ext": "", "files": 1, "bytes": 5},
            {"ext": "zip", "files": 1, "bytes": zf.stat().st_size},
        ]
        # Each ext works as is in the ext filter of /search.
        hits = c.get("/search", params={"q": "", "ext": "md", "folder": str(tmp_path)}).json()["hits"]
        assert len(hits) == 2
        assert c.get(f"/roots/{oid}/extensions").json() == [{"ext": "txt", "files": 1, "bytes": 1}]
        assert c.get("/roots/999999/extensions").status_code == 404


def test_largest_files_of_1_folder(conn, tmp_path):
    write(tmp_path / "small.txt", "1")
    write(tmp_path / "docs" / "big.md", "x" * 50)
    write(tmp_path / "docs" / "mid-a.txt", "x" * 20)
    write(tmp_path / "mid-b.txt", "x" * 20)
    zf = tmp_path / "old.zip"
    with zipfile.ZipFile(zf, "w") as z:
        z.writestr("huge.txt", "y" * 5000)
    with client() as c:
        rid = c.post("/roots", json={"path": str(tmp_path)}).json()["id"]
        assert c.get(f"/roots/{rid}/largest").json() == []
        c.post(f"/roots/{rid}/scan")
        c.post("/jobs/run")
        got = c.get(f"/roots/{rid}/largest").json()
        # The zip counts at its own size (stored, so over 5000 bytes), and its entry is not listed. A tie goes by path.
        assert zf.stat().st_size > 5000
        assert [(h["path"], h["size"]) for h in got] == [
            (str(zf), zf.stat().st_size),
            (str(tmp_path / "docs" / "big.md"), 50),
            (str(tmp_path / "docs" / "mid-a.txt"), 20),
            (str(tmp_path / "mid-b.txt"), 20),
            (str(tmp_path / "small.txt"), 1),
        ]
        assert all(h["cloud"] == "local" and h["mtime"] for h in got)
        assert len(c.get(f"/roots/{rid}/largest", params={"limit": 2}).json()) == 2
        # ?ext= keeps only those endings, as /search reads them: repeatable, comma-separated, any case, dots optional.
        txt = [(str(tmp_path / "docs" / "mid-a.txt"), 20), (str(tmp_path / "mid-b.txt"), 20), (str(tmp_path / "small.txt"), 1)]
        for params in ({"ext": "txt"}, {"ext": ".TXT"}, [("ext", "txt"), ("ext", "txt")]):
            assert [(h["path"], h["size"]) for h in c.get(f"/roots/{rid}/largest", params=params).json()] == txt
        both = c.get(f"/roots/{rid}/largest", params={"ext": "md,zip"}).json()
        assert [h["path"] for h in both] == [str(zf), str(tmp_path / "docs" / "big.md")]
        assert c.get(f"/roots/{rid}/largest", params={"ext": "pdf"}).json() == []
        assert len(c.get(f"/roots/{rid}/largest", params={"ext": "txt", "limit": 1}).json()) == 1
        assert c.get(f"/roots/{rid}/largest", params={"limit": 0}).status_code == 422
        assert c.get("/roots/999999/largest").status_code == 404


def test_failed_jobs_can_be_queued_again(conn, tmp_path):
    write(tmp_path / "good.txt", "fine")
    (tmp_path / "bad.txt").write_bytes(b"caf\xe9")
    with client() as c:
        root = c.post("/roots", json={"path": str(tmp_path)}).json()["id"]
        c.post(f"/roots/{root}/scan")
        c.post("/jobs/run")
        assert c.get("/jobs").json()["failed"] == 1
        # Fix the file, then queue its job again.
        write(tmp_path / "bad.txt", "café")
        assert c.post("/jobs/failed/retry").json() == {"requeued": 1}
        counts = c.get("/jobs").json()
        assert (counts["failed"], counts["queued"], counts["done"]) == (0, 1, 1)
        assert c.get("/jobs/failed").json() == []
        assert c.post("/jobs/run").json()["processed"] == 1
        assert c.get("/jobs").json()["done"] == 2
        # Nothing failed, nothing to queue.
        assert c.post("/jobs/failed/retry").json() == {"requeued": 0}
        hits = c.get("/search", params={"q": "café", "mode": "words"}).json()["hits"]
        assert [h["path"] for h in hits] == [str(tmp_path / "bad.txt")]
