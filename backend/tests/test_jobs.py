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


def _stick(conn, minutes: int, attempts: int) -> None:
    """Leave the only job in 'running', as a worker that died would."""
    conn.execute(
        "UPDATE jobs SET state = 'running', attempts = %s, "
        "updated_at = now() - make_interval(mins => %s)", (attempts, minutes))


def test_stuck_job_goes_back_in_the_queue(conn, tmp_path):
    write(tmp_path / "a.txt", "alpha")
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)
    _stick(conn, minutes=31, attempts=1)
    assert jobs.requeue_stuck(conn) == (1, 0)
    state, attempts, error = conn.execute(
        "SELECT state, attempts, error FROM jobs").fetchone()
    assert (state, attempts) == ("queued", 1)
    assert error == "re-queued after attempt 1: running for more than 30 minutes"
    # The next run claims it again, which counts as attempt 2, and finishes it.
    assert jobs.run(conn) == 1
    state, attempts, error = conn.execute(
        "SELECT state, attempts, error FROM jobs").fetchone()
    assert (state, attempts, error) == ("done", 2, None)
    (text,) = conn.execute("SELECT content FROM files").fetchone()
    assert text == "alpha"


def test_running_job_within_the_time_out_stays(conn, tmp_path):
    write(tmp_path / "a.txt", "alpha")
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)
    _stick(conn, minutes=29, attempts=1)
    assert jobs.requeue_stuck(conn) == (0, 0)
    assert jobs.run(conn) == 0
    assert jobs.summary(conn)["running"] == 1


def test_job_fails_for_good_after_3_attempts(conn, tmp_path):
    write(tmp_path / "a.txt", "alpha")
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)
    _stick(conn, minutes=31, attempts=jobs.MAX_ATTEMPTS)
    assert jobs.run(conn) == 0
    state, attempts, error = conn.execute(
        "SELECT state, attempts, error FROM jobs").fetchone()
    assert (state, attempts) == ("failed", 3)
    assert error == "gave up after 3 attempts: running for more than 30 minutes each time"


def test_time_out_can_be_set(conn, tmp_path):
    from datetime import timedelta

    write(tmp_path / "a.txt", "alpha")
    write(tmp_path / "b.txt", "beta")
    rid = indexer.add_root(conn, str(tmp_path))
    indexer.scan_root(conn, rid)
    conn.execute("UPDATE jobs SET state = 'running', attempts = 1, "
                 "updated_at = now() - interval '6 minutes' WHERE id = 1")
    conn.execute("UPDATE jobs SET state = 'running', attempts = 2, "
                 "updated_at = now() - interval '4 minutes' WHERE id = 2")
    assert jobs.requeue_stuck(conn, timedelta(minutes=5)) == (1, 0)
    rows = conn.execute("SELECT id, state, error FROM jobs ORDER BY id").fetchall()
    assert rows == [
        (1, "queued", "re-queued after attempt 1: running for more than 5 minutes"),
        (2, "running", None),
    ]
