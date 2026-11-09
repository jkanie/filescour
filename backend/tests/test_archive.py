"""Zip entries as rows of their own: paths, text, search, export, re-indexing,
the online-only guard, the guards against hostile zips."""

import io
import json
import shutil
import struct
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from filescour import archive, cloud, db, export, extract, fetch, indexer, jobs
from filescour.search import Filters, search

from .helpers import pdf_with_text, write, zip_bytes

WHEN = datetime(2024, 5, 31, 16, 8, 38, tzinfo=timezone.utc)  # helpers.ZIP_DATE
NS = 1_700_000_000_000_000_000
ONLINE = cloud.RECALL_ON_DATA_ACCESS | cloud.UNPINNED

MAY = "Invoice 4417 attached.\nsee you\n"
BACKUP = [
    ("notes/", ""),
    ("notes/may.txt", MAY),
    ("scan.jpg", b"\xff\xd8\x00"),
    ("report.pdf", pdf_with_text("Receipt 9921 paid")),
    ("latin.txt", b"caf\xe9"),
]


def index(conn, root, **kw):
    rid = indexer.add_root(conn, str(root), **kw)
    result = indexer.scan_root(conn, rid)
    jobs.run(conn)
    return rid, result


def entries(conn):
    """{path inside the zip: (name, ext, size, mtime, content, source, cloud)}."""
    return {
        path.split("!/", 1)[1]: rest
        for path, *rest in conn.execute(
            "SELECT f.path, f.name, f.ext, f.size, f.mtime, f.content, f.content_source, "
            "f.cloud FROM files f WHERE f.parent_id IS NOT NULL ORDER BY f.path"
        )
    }


def job_states(conn):
    return conn.execute("SELECT state, error FROM jobs ORDER BY id").fetchall()


@pytest.fixture
def fake_windows(monkeypatch):
    """Attributes by path. A path not listed reads as a plain local file (0)."""
    attrs: dict[str, int] = {}
    monkeypatch.setattr(cloud, "attributes_of", lambda st, path: attrs.get(path, 0))

    def now(path):
        a = attrs.get(path, 0)
        return cloud.state_from_attributes(a), a

    monkeypatch.setattr(cloud, "current_state", now)
    return attrs


@pytest.fixture
def opened(monkeypatch):
    """Every zip that a worker opens."""
    seen: list[str] = []
    real = archive.open_zip

    def spy(path):
        seen.append(path)
        return real(path)

    monkeypatch.setattr(archive, "open_zip", spy)
    return seen


@pytest.fixture
def read_entries(monkeypatch):
    """Every zip entry whose data is read."""
    seen: list[str] = []
    real = zipfile.ZipFile.open

    def spy(self, name, mode="r", *args, **kwargs):
        if mode == "r":  # not the writes that build the test zips
            seen.append(getattr(name, "filename", name))
        return real(self, name, mode, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    return seen


def test_entries_get_rows_with_their_text(conn, tmp_path):
    zp = write(tmp_path / "old" / "backup.zip", zip_bytes(BACKUP))
    _, r = index(conn, tmp_path)
    assert (r.added, r.queued) == (1, 1)
    assert job_states(conn) == [("done", None)]
    got = entries(conn)
    assert list(got) == ["latin.txt", "notes/may.txt", "report.pdf", "scan.jpg"]
    assert got["notes/may.txt"] == ["may.txt", "txt", len(MAY), WHEN, MAY, "file", "local"]
    assert got["scan.jpg"][:6] == ["scan.jpg", "jpg", 3, WHEN, None, None]
    assert "Receipt 9921 paid" in got["report.pdf"][4]
    assert got["report.pdf"][5] == "pdf"
    assert got["latin.txt"][4] is None  # not UTF-8, the same as a file on disk
    (path, content, indexed) = conn.execute(
        "SELECT path, content, indexed_at IS NOT NULL FROM files WHERE parent_id IS NULL"
    ).fetchone()
    assert (path, content, indexed) == (str(zp), None, True)


def test_every_search_mode_finds_entries(conn, tmp_path):
    zp = str(write(tmp_path / "backup.zip", zip_bytes(BACKUP)))
    write(tmp_path / "may.md", "nothing here\n")
    index(conn, tmp_path)
    inner = zp + "!/notes/may.txt"

    hits = search(conn, "may", "name").hits
    assert [(h.path, h.archive) for h in hits] == [(inner, zp), (str(tmp_path / "may.md"), None)]
    assert [h.path for h in search(conn, "backup.zip!/notes", "path").hits] == [inner]
    (hit,) = search(conn, "^invoice", "regex").hits
    assert (hit.path, [(ln.line, ln.text) for ln in hit.lines]) == (
        inner, [(1, "Invoice 4417 attached.")])
    (hit,) = search(conn, "receipt", "words").hits
    assert (hit.path, hit.source) == (zp + "!/report.pdf", "pdf")
    assert [h.name for h in search(conn, "", "name", Filters(exts=("jpg",))).hits] == ["scan.jpg"]
    assert [h.name for h in search(conn, "", "name", Filters(source="pdf")).hits] == ["report.pdf"]
    size = Filters(min_size=len(MAY), max_size=len(MAY))
    assert [h.name for h in search(conn, "", "name", size).hits] == ["may.txt"]
    assert [h.name for h in search(conn, "", "name", Filters(folder=str(tmp_path))).hits] == [
        "backup.zip", "latin.txt", "may.txt", "report.pdf", "scan.jpg", "may.md"]
    before = Filters(modified_before=WHEN + timedelta(seconds=1))
    assert search(conn, "", "name", before).total == 4  # the entries keep their zip dates


def test_export_includes_entries(conn, tmp_path):
    zp = str(write(tmp_path / "backup.zip", zip_bytes(BACKUP)))
    write(tmp_path / "ledger.csv", "4417\n", mtime_ns=1_717_171_717_123_456_000)
    index(conn, tmp_path)
    result = search(conn, "4417", "regex")
    lines = export.to_csv(result).splitlines()
    assert lines[2:] == [
        f"{zp}!/notes/may.txt,may.txt,txt,{len(MAY)},2024-05-31T16:08:38+00:00,"
        "file,local,1,Invoice 4417 attached.",
        f"{tmp_path}/ledger.csv,ledger.csv,csv,5,2024-05-31T16:08:37.123456+00:00,"
        "file,local,1,4417",
    ]
    doc = json.loads(export.to_json(result))
    assert [(h["path"], h["archive"]) for h in doc["hits"]] == [
        (zp + "!/notes/may.txt", zp), (f"{tmp_path}/ledger.csv", None)]


def test_reindexing_replaces_the_entries_of_a_changed_or_deleted_zip(conn, tmp_path):
    zp = write(tmp_path / "backup.zip", zip_bytes([("a.txt", "old words\n")]), mtime_ns=NS)
    rid, _ = index(conn, tmp_path)
    assert list(entries(conn)) == ["a.txt"]

    again = indexer.scan_root(conn, rid)  # unchanged: the entries stay, no job
    assert (again.unchanged, again.queued, again.removed) == (1, 0, 0)
    assert list(entries(conn)) == ["a.txt"]

    write(zp, zip_bytes([("b.txt", "new words\n")]), mtime_ns=NS + 10**9)
    changed = indexer.scan_root(conn, rid)
    assert (changed.changed, changed.queued, changed.removed) == (1, 1, 0)
    assert entries(conn) == {}  # stale entries go at the scan
    jobs.run(conn)
    assert list(entries(conn)) == ["b.txt"]
    assert search(conn, "old", "regex").hits == []
    assert [h.name for h in search(conn, "new", "words").hits] == ["b.txt"]

    zp.unlink()
    gone = indexer.scan_root(conn, rid)
    assert gone.removed == 1
    (count,) = conn.execute("SELECT count(*) FROM files").fetchone()
    assert count == 0


def test_rescan_matches_a_fresh_scan(conn, tmp_path):
    write(tmp_path / "a.zip", zip_bytes([("x.txt", "one\n"), ("y.txt", "two\n")]), mtime_ns=NS)
    zb = write(tmp_path / "b.zip", zip_bytes([("z.txt", "three\n")]), mtime_ns=NS)
    rid, _ = index(conn, tmp_path)
    write(zb, zip_bytes([("z.txt", "four\n")]), mtime_ns=NS + 10**9)
    indexer.scan_root(conn, rid)
    jobs.run(conn)
    updated = entries(conn)
    conn.execute("TRUNCATE roots, files, jobs, fetches RESTART IDENTITY CASCADE")
    index(conn, tmp_path)
    assert entries(conn) == updated
    assert updated["z.txt"][4] == "four\n"


def test_an_online_zip_is_never_opened(conn, tmp_path, fake_windows, opened):
    zp = str(write(tmp_path / "backup.zip", zip_bytes(BACKUP)))
    fake_windows[zp] = ONLINE
    rid, r = index(conn, tmp_path)
    assert (r.queued, r.held) == (0, 1)
    assert opened == []
    assert entries(conn) == {}
    (hit,) = search(conn, "backup", "name").hits
    assert (hit.cloud, hit.archive) == ("online", None)
    # Brought down to the disk: the next scan queues it, and it is expanded.
    fake_windows[zp] = cloud.PINNED
    assert indexer.scan_root(conn, rid).queued == 1
    jobs.run(conn)
    assert opened == [zp]
    # Back online: its entries keep their text, and they read as online too.
    fake_windows[zp] = ONLINE
    assert indexer.scan_root(conn, rid).queued == 0
    found = search(conn, "", "name", Filters(cloud="online")).hits
    assert [h.name for h in found] == ["backup.zip", "latin.txt", "may.txt", "report.pdf",
                                       "scan.jpg"]
    assert [h.name for h in search(conn, "4417", "regex").hits] == ["may.txt"]
    # A zip entry is never down-loaded on its own, and a zip has no text to fetch.
    got = fetch.request(conn, [zp + "!/notes/may.txt", zp], "temporary")
    assert (got.queued, got.not_online, got.no_text) == (0, 0, 1)


def test_the_worker_checks_the_zip_again_before_it_opens_it(conn, tmp_path, fake_windows,
                                                            opened):
    zp = str(write(tmp_path / "backup.zip", zip_bytes(BACKUP)))
    rid = indexer.add_root(conn, str(tmp_path))
    assert indexer.scan_root(conn, rid).queued == 1
    fake_windows[zp] = ONLINE  # it went online-only after the scan
    jobs.run(conn)
    assert opened == []
    assert job_states(conn) == [("skipped", "not opened: online only")]
    assert entries(conn) == {}


def test_a_zip_on_a_windows_drive_is_read_only_when_the_folder_allows_it(conn, tmp_path,
                                                                         monkeypatch, opened):
    monkeypatch.setattr(cloud, "windows_drives", lambda: [str(tmp_path)])
    write(tmp_path / "a" / "backup.zip", zip_bytes([("x.txt", "x\n")]))
    write(tmp_path / "b" / "backup.zip", zip_bytes([("y.txt", "y\n")]))
    guarded = indexer.add_root(conn, str(tmp_path / "a"))
    allowed = indexer.add_root(conn, str(tmp_path / "b"), extract_unknown=True)
    assert indexer.scan_root(conn, guarded).held == 1
    assert indexer.scan_root(conn, allowed).queued == 1
    jobs.run(conn)
    assert opened == [str(tmp_path / "b" / "backup.zip")]
    assert {k: v[6] for k, v in entries(conn).items()} == {"y.txt": "unknown"}


def test_too_many_entries(conn, tmp_path, monkeypatch, read_entries):
    monkeypatch.setattr(archive, "MAX_ENTRIES", 3)
    write(tmp_path / "many.zip", zip_bytes([(f"{i}.txt", "x") for i in range(4)]))
    write(tmp_path / "few.zip", zip_bytes([(f"{i}.txt", "y") for i in range(3)]))
    index(conn, tmp_path)
    assert job_states(conn) == [("done", None), ("failed", "more than 3 entries")]
    assert sorted(read_entries) == ["0.txt", "1.txt", "2.txt"]  # all from few.zip
    assert [h.name for h in search(conn, "many", "name").hits] == ["many.zip"]
    assert list(entries(conn)) == ["0.txt", "1.txt", "2.txt"]


def test_a_zip_bomb_is_refused_before_any_entry_is_read(conn, tmp_path, monkeypatch,
                                                        read_entries):
    monkeypatch.setattr(archive, "MAX_TOTAL_BYTES", 1024 * 1024)
    zeros = b"0" * (1024 * 1024 + 1)  # deflate packs it into under 5,000 bytes
    data = zip_bytes([("a.txt", "fine\n"), ("zeros.txt", zeros)])
    assert len(data) < 5000
    write(tmp_path / "bomb.zip", data)
    # Large entries that are never read do not count.
    write(tmp_path / "photos.zip", zip_bytes([("big.jpg", zeros), ("cap.txt", "ok\n")]))
    index(conn, tmp_path)
    states = dict(zip(["bomb.zip", "photos.zip"], job_states(conn)))
    assert states["bomb.zip"] == (
        "failed", "text-type entries hold more than 1 MB uncompressed")
    assert states["photos.zip"] == ("done", None)
    assert read_entries == ["cap.txt"]
    assert list(entries(conn)) == ["big.jpg", "cap.txt"]


def test_encrypted_entries_are_listed_but_never_read(conn, tmp_path, read_entries):
    write(tmp_path / "locked.zip", zip_bytes([("secret.txt", "hidden 4417\n"),
                                              ("open.txt", "plain\n")],
                                             encrypted=("secret.txt",)))
    index(conn, tmp_path)
    assert job_states(conn) == [("done", None)]
    assert read_entries == ["open.txt"]
    got = entries(conn)
    assert (got["secret.txt"][4], got["open.txt"][4]) == (None, "plain\n")
    assert search(conn, "4417", "regex").hits == []


def test_names_that_point_outside_the_zip_get_no_row(conn, tmp_path, read_entries):
    bad = ["../evil.txt", "/abs.txt", "a/../../b.txt", "a\\..\\b.txt", "C:/x.txt",
           "./x.txt", "a//b.txt"]
    data = zip_bytes([(n, "evil\n") for n in bad] + [("ok.txt", "fine\n"),
                                                    (".hidden.txt", "dot\n"),
                                                    ("__MACOSX/._ok.txt", "dot\n")])
    zp = write(tmp_path / "t.zip", data)
    with zipfile.ZipFile(zp) as zf:
        infos, rejected, encrypted = archive.plan(zf)
    assert ([i.filename for i in infos], rejected, encrypted) == (["ok.txt"], 7, 0)
    index(conn, tmp_path)
    assert list(entries(conn)) == ["ok.txt"]
    assert read_entries == ["ok.txt"]
    for name in bad:
        assert not archive.safe_name(name), name
    assert archive.safe_name("notes/2026/may.txt")


def test_an_entry_that_holds_more_than_it_declares_gets_no_text(conn, tmp_path):
    data = bytearray(zip_bytes([("a.txt", "x" * 100), ("b.txt", "fine\n")]))
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf:
        local = zf.getinfo("a.txt").header_offset
    struct.pack_into("<I", data, local + 22, 10)  # uncompressed size, local header
    central = data.find(b"PK\x01\x02")  # a.txt comes 1st
    struct.pack_into("<I", data, central + 24, 10)  # uncompressed size, central directory
    write(tmp_path / "liar.zip", bytes(data))
    index(conn, tmp_path)
    got = entries(conn)
    assert (got["a.txt"][2], got["a.txt"][4]) == (10, None)
    assert got["b.txt"][4] == "fine\n"


def test_a_nested_zip_is_listed_but_not_expanded(conn, tmp_path):
    inner = zip_bytes([("deep.txt", "deep text\n")])
    write(tmp_path / "outer.zip", zip_bytes([("old/inner.zip", inner)]))
    index(conn, tmp_path)
    got = entries(conn)
    assert list(got) == ["old/inner.zip"]
    assert got["old/inner.zip"][1:3] == ["zip", len(inner)]
    assert search(conn, "deep", "path").hits == []
    assert len(job_states(conn)) == 1


def test_a_broken_zip_fails_and_keeps_its_name(conn, tmp_path):
    write(tmp_path / "broken.zip", b"not a zip at all")
    index(conn, tmp_path)
    ((state, error),) = job_states(conn)
    assert state == "failed"
    assert error.startswith("not a readable zip")
    assert [h.name for h in search(conn, "broken", "name").hits] == ["broken.zip"]
    assert [h.name for h in search(conn, "broken.zip", "path").hits] == ["broken.zip"]


def test_entry_details(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(extract, "MAX_CHARS", 5)
    zp = write(tmp_path / "d.zip", zip_bytes([("long.txt", "0123456789"),
                                              ("long.txt", "a 2nd entry, same name")]),
               mtime_ns=NS)
    write(tmp_path / "e.zip", zip_bytes([("when.txt", "x")], date_time=(1980, 0, 0, 0, 0, 0)),
          mtime_ns=NS)
    index(conn, tmp_path)
    (truncated,) = conn.execute(
        "SELECT truncated FROM files WHERE path = %s", (str(zp) + "!/long.txt",)
    ).fetchone()
    got = entries(conn)
    assert (got["long.txt"][4], truncated) == ("01234", True)  # the 1st one is kept
    # Month 0 is no date: the zip's own modified time stands in.
    assert got["when.txt"][3] == indexer.EPOCH + timedelta(microseconds=NS // 1000)


def test_a_real_file_wins_over_an_entry_at_the_same_path(conn, tmp_path):
    write(tmp_path / "b.zip", zip_bytes([("x.txt", "from the zip\n")]))
    write(tmp_path / "b.zip!" / "x.txt", "from the disk\n")
    rid, _ = index(conn, tmp_path)
    assert search(conn, "from", "regex").hits[0].lines[0].text == "from the disk"
    assert entries(conn) == {}
    # The other way round: the entry came first, then a real file at its path.
    conn.execute("TRUNCATE roots, files, jobs, fetches RESTART IDENTITY CASCADE")
    shutil.rmtree(tmp_path / "b.zip!")
    rid, _ = index(conn, tmp_path)
    assert list(entries(conn)) == ["x.txt"]
    write(tmp_path / "b.zip!" / "x.txt", "from the disk\n")
    assert indexer.scan_root(conn, rid).added == 1
    jobs.run(conn)
    (hit,) = search(conn, "from", "regex").hits
    assert (hit.archive, hit.lines[0].text) == (None, "from the disk")


def test_the_api_shows_entries(conn, tmp_path):
    from fastapi.testclient import TestClient

    from filescour.api import app

    zp = str(write(tmp_path / "backup.zip", zip_bytes(BACKUP)))
    write(tmp_path / "plain.txt", "plain\n")
    with TestClient(app) as c:
        rid = c.post("/roots", json={"path": str(tmp_path)}).json()["id"]
        assert c.post(f"/roots/{rid}/scan").json()["queued"] == 2
        assert c.post("/jobs/run").json()["done"] == 2
        assert c.get("/roots").json() == [{"id": rid, "path": str(tmp_path),
                                           "extract_unknown": False, "files": 2,
                                           "online": 0, "unknown": 0, "zip_entries": 4}]
        (hit,) = c.get("/search", params={"q": "^see", "mode": "regex"}).json()["hits"]
        assert (hit["path"], hit["archive"]) == (zp + "!/notes/may.txt", zp)
        exp = c.get("/export", params={"format": "json", "q": "see", "mode": "words"})
        assert json.loads(exp.text)["hits"][0]["archive"] == zp


def test_the_schema_brings_an_older_database_up_to_date(conn):
    conn.execute("ALTER TABLE files DROP COLUMN parent_id")
    db.init_schema(conn)
    db.init_schema(conn)  # safe to run more than once
    (cols,) = conn.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'files' AND column_name = 'parent_id'"
    ).fetchone()
    (fks,) = conn.execute(
        "SELECT count(*) FROM pg_constraint WHERE conrelid = 'files'::regclass "
        "AND contype = 'f' AND conkey = ARRAY[(SELECT attnum FROM pg_attribute "
        "WHERE attrelid = 'files'::regclass AND attname = 'parent_id')]"
    ).fetchone()
    assert (cols, fks) == (1, 1)
