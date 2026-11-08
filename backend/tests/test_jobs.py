import threading

from filescour import db, indexer, jobs

from .helpers import write


def test_parallel_workers_never_share_a_job(conn, tmp_path, test_url):
    for i in range(300):
        write(tmp_path / f"f{i:03}.txt", f"text {i}")
    rid = indexer.add_root(conn, str(tmp_path))
    assert indexer.scan_root(conn, rid).queued == 300

    claimed: list[list[int]] = [[] for _ in range(4)]

    def worker(slot: int) -> None:
        with db.connect(test_url) as c:
            while (job := jobs.claim(c)) is not None:
                claimed[slot].append(job[0])
                jobs.process(c, *job)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    every = [j for slot in claimed for j in slot]
    assert len(every) == 300
    assert len(set(every)) == 300
    assert jobs.summary(conn) == {"queued": 0, "running": 0, "done": 300, "failed": 0,
                                   "skipped": 0}
    (filled,) = conn.execute("SELECT count(*) FROM files WHERE content IS NOT NULL").fetchone()
    assert filled == 300


def test_failed_extract_is_recorded(conn, tmp_path):
    write(tmp_path / "bad.txt", b"caf\xe9")
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)
    assert jobs.run(conn) == 1
    state, error = conn.execute("SELECT state, error FROM jobs").fetchone()
    assert (state, error) == ("failed", "not UTF-8 text")
    (indexed,) = conn.execute("SELECT indexed_at IS NOT NULL FROM files").fetchone()
    assert indexed


def test_rescan_replaces_a_queued_job(conn, tmp_path):
    p = write(tmp_path / "a.txt", "one", mtime_ns=1_700_000_000_000_000_000)
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)
    write(p, "two", mtime_ns=1_700_000_001_000_000_000)
    indexer.scan_root(conn, rid)
    assert jobs.summary(conn)["queued"] == 1
