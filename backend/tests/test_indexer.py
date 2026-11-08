import os

from filescour import indexer, jobs

from .helpers import write


def rows(conn):
    return sorted(conn.execute(
        "SELECT path, name, ext, size, mtime, content, content_source, truncated "
        "FROM files"
    ).fetchall())


def index(conn, root):
    rid = indexer.add_root(conn, str(root))
    result = indexer.scan_root(conn, rid)
    jobs.run(conn)
    return rid, result


def test_first_scan_records_files_and_queues_text(conn, tmp_path):
    write(tmp_path / "a" / "notes.md", "alpha\n")
    write(tmp_path / "a" / "photo.JPG", b"\xff\xd8\xff")
    write(tmp_path / ".git" / "config", "skip me")
    write(tmp_path / "a" / ".hidden.txt", "skip me too")
    rid, result = index(conn, tmp_path)
    assert (result.added, result.queued, result.removed) == (2, 1, 0)
    got = {r[1]: r for r in rows(conn)}
    assert set(got) == {"notes.md", "photo.JPG"}
    assert got["photo.JPG"][2] == "jpg"
    assert got["notes.md"][5:7] == ("alpha\n", "file")
    assert got["photo.JPG"][5] is None


def test_symbolic_links_are_not_followed(conn, tmp_path):
    write(tmp_path / "real" / "x.txt", "x")
    os.symlink(tmp_path / "real", tmp_path / "loop")
    os.symlink(tmp_path / "real" / "x.txt", tmp_path / "link.txt")
    index(conn, tmp_path)
    assert [r[1] for r in rows(conn)] == ["x.txt"]


def test_unchanged_files_are_skipped(conn, tmp_path):
    write(tmp_path / "a.txt", "one")
    rid, _ = index(conn, tmp_path)
    again = indexer.scan_root(conn, rid)
    assert (again.added, again.changed, again.unchanged, again.queued) == (0, 0, 1, 0)


def test_changed_and_removed_files(conn, tmp_path):
    a = write(tmp_path / "a.txt", "old text", mtime_ns=1_700_000_000_000_000_000)
    b = write(tmp_path / "b.txt", "bye")
    rid, _ = index(conn, tmp_path)
    write(a, "new text here", mtime_ns=1_700_000_100_000_000_000)
    b.unlink()
    write(tmp_path / "c.txt", "hello")
    result = indexer.scan_root(conn, rid)
    assert (result.added, result.changed, result.removed, result.queued) == (1, 1, 1, 2)
    jobs.run(conn)
    got = {r[1]: r[5] for r in rows(conn)}
    assert got == {"a.txt": "new text here", "c.txt": "hello"}


def test_rescan_gives_the_same_index_as_a_fresh_scan(conn, tmp_path, test_url):
    for i in range(12):
        write(tmp_path / f"d{i % 3}" / f"f{i}.txt", f"file {i}\n" * (i + 1),
              mtime_ns=1_700_000_000_000_000_000 + i * 1_000_123)
    rid, _ = index(conn, tmp_path)
    write(tmp_path / "d0" / "f0.txt", "changed", mtime_ns=1_800_000_000_000_000_000)
    (tmp_path / "d1" / "f1.txt").unlink()
    write(tmp_path / "d2" / "new.txt", "brand new")
    indexer.scan_root(conn, rid)
    jobs.run(conn)
    updated = rows(conn)

    conn.execute("TRUNCATE roots, files, jobs, fetches RESTART IDENTITY CASCADE")
    index(conn, tmp_path)
    assert rows(conn) == updated


def test_microseconds_survive_the_round_trip(conn, tmp_path):
    ns = 1_712_345_678_123_456_789
    write(tmp_path / "t.txt", "t", mtime_ns=ns)
    rid, _ = index(conn, tmp_path)
    assert indexer.scan_root(conn, rid).unchanged == 1
    (mtime,) = conn.execute("SELECT mtime FROM files").fetchone()
    assert int(mtime.timestamp() * 1_000_000) == ns // 1000
