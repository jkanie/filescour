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
        rid = root.json()["id"]
        assert c.post(f"/roots/{rid}/scan").json()["queued"] == 1
        ran = c.post("/jobs/run").json()
        assert (ran["processed"], ran["requeued"], ran["gave_up"], ran["done"]) == (1, 0, 0, 1)
        assert c.get("/roots").json() == [{"id": rid, "path": str(tmp_path),
                                           "extract_unknown": False, "files": 2,
                                           "online": 0, "unknown": 0,
                                           "zip_entries": 0}]

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
